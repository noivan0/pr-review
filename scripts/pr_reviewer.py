"""pr_reviewer.py — 서버 없이 GitHub Actions에서 실행되는 독립형 PR 리뷰 스크립트

사용법:
    # GitHub Actions (GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH 자동 제공)
    uv run python scripts/pr_reviewer.py

    # 로컬 테스트
    GITHUB_TOKEN=ghp_xxx \\
    GITHUB_REPOSITORY=owner/repo \\
    PR_NUMBER=1 \\
    PR_TITLE="feat: add feature" \\
    HEAD_SHA=abc123 \\
    ANTHROPIC_API_KEY=sk-ant-xxx \\
    uv run python scripts/pr_reviewer.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from anthropic import APIConnectionError, APITimeoutError, AsyncAnthropic, RateLimitError

# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("pr_reviewer")

# ---------------------------------------------------------------------------
# 환경 변수 / 설정
# ---------------------------------------------------------------------------

GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")   # "owner/repo" (Actions 자동 제공)
EVENT_PATH = os.getenv("GITHUB_EVENT_PATH", "")     # PR 이벤트 JSON 경로

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN")  # 내부 API 사용 시
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL")       # 내부 API 사용 시

MAX_FILE_LINES = int(os.getenv("MAX_FILE_LINES", "500"))
MAX_TOTAL_LINES = int(os.getenv("MAX_TOTAL_LINES", "3000"))
LANGUAGE = os.getenv("REVIEW_LANGUAGE", "ko")
MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))

GITHUB_API_BASE = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ANTHROPIC_BASE_URL이 /messages로 끝나면 SDK URL 조립을 건너뛰고 httpx 직접 호출
# (SDK가 base_url + /v1/messages를 붙여 이중 경로 404 발생하는 케이스 대응)
_DIRECT_ENDPOINT_MODE: bool = bool(
    ANTHROPIC_BASE_URL and ANTHROPIC_BASE_URL.rstrip("/").endswith("/messages")
)

# ---------------------------------------------------------------------------
# 시스템 프롬프트 (review_service.py에서 재사용)
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT_KO = """당신은 시니어 코드 리뷰어입니다. PR diff를 분석하여 개발자에게 유용한 피드백을 제공합니다.

## 리뷰 체크리스트
1. **버그/로직 오류** — 잘못된 조건문, 경계값 오류, 상태 오류
2. **보안 취약점** — OWASP Top 10 (XSS, SQL Injection, 인증/인가 결함, 민감 데이터 노출)
3. **성능 문제** — N+1 쿼리, 메모리 누수, 불필요한 반복, 대용량 데이터 처리
4. **코드 품질** — DRY 원칙, 단일 책임, 가독성, 코드 중복
5. **에러 핸들링** — 예외 처리 누락, 적절하지 않은 에러 메시지
6. **테스트 가능성** — 테스트하기 어려운 코드 구조

## 출력 형식 (JSON 배열만 출력, 다른 텍스트 절대 금지)
```json
[
  {
    "path": "src/auth.py",
    "line": 42,
    "side": "RIGHT",
    "category": "security",
    "severity": "critical",
    "body": "**문제**: SQL 인젝션 취약점\\n\\n입력값을 직접 쿼리에 삽입하고 있습니다.\\n\\n**수정 방법:**\\n```python\\n# Before\\nquery = f'SELECT * FROM users WHERE id = {user_id}'\\n# After\\nquery = 'SELECT * FROM users WHERE id = %s'\\ncursor.execute(query, (user_id,))\\n```"
  }
]
```

## 규칙
- 문제가 없으면 빈 배열 `[]` 반환
- severity: critical (보안/데이터 손실) > major (버그/성능) > minor (품질) > info (제안)
- 각 코멘트는 문제 설명 + 수정 방법 코드 블록 포함
- 한국어로 설명, 코드는 영어
- 실제 diff에 존재하는 파일/라인만 참조할 것"""

REVIEW_SYSTEM_PROMPT_EN = """You are a senior code reviewer. Analyze the PR diff and provide useful feedback.

## Review Checklist
1. **Bugs/Logic errors** — wrong conditions, off-by-one, state errors
2. **Security vulnerabilities** — OWASP Top 10 (XSS, SQL Injection, auth flaws, data exposure)
3. **Performance issues** — N+1 queries, memory leaks, unnecessary loops
4. **Code quality** — DRY principle, single responsibility, readability
5. **Error handling** — missing exceptions, inappropriate error messages
6. **Testability** — code that is hard to test

## Output Format (JSON array ONLY, no other text)
```json
[
  {
    "path": "src/auth.py",
    "line": 42,
    "side": "RIGHT",
    "category": "security",
    "severity": "critical",
    "body": "**Issue**: SQL injection vulnerability\\n\\n**Fix:**\\n```python\\nquery = 'SELECT * FROM users WHERE id = %s'\\ncursor.execute(query, (user_id,))\\n```"
  }
]
```

## Rules
- Return empty array `[]` if no issues found
- severity: critical > major > minor > info
- Include problem description + fix code block in each comment
- Only reference files/lines that exist in the diff"""

# ---------------------------------------------------------------------------
# 데이터 클래스 (Pydantic 없이 독립 실행)
# ---------------------------------------------------------------------------


@dataclass
class FileDiff:
    path: str
    patch: str | None = None
    additions: int = 0
    deletions: int = 0
    status: str = "modified"


@dataclass
class PRDiff:
    files: list[FileDiff] = field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
    truncated: bool = False


@dataclass
class ReviewComment:
    path: str
    line: int | None
    side: str
    category: str
    severity: str
    body: str


# ---------------------------------------------------------------------------
# PR 메타데이터 추출
# ---------------------------------------------------------------------------


def load_pr_info() -> dict[str, Any]:
    """GITHUB_EVENT_PATH JSON → PR 메타데이터 (Actions 환경)
    로컬 테스트: PR_NUMBER, PR_TITLE, HEAD_SHA 환경변수로 대체.
    """
    if EVENT_PATH and Path(EVENT_PATH).exists():
        event = json.loads(Path(EVENT_PATH).read_text(encoding="utf-8"))
        pr = event.get("pull_request", {})
        return {
            "number": pr["number"],
            "title": pr.get("title", ""),
            "body": pr.get("body", "") or "",
            "head_sha": pr["head"]["sha"],
            "draft": pr.get("draft", False),
        }

    # 로컬 테스트 fallback
    pr_number = os.environ.get("PR_NUMBER")
    head_sha = os.environ.get("HEAD_SHA")
    if not pr_number or not head_sha:
        raise RuntimeError(
            "PR_NUMBER, HEAD_SHA 환경변수가 필요합니다. "
            "또는 GITHUB_EVENT_PATH를 설정하세요."
        )
    return {
        "number": int(pr_number),
        "title": os.getenv("PR_TITLE", ""),
        "body": os.getenv("PR_BODY", ""),
        "head_sha": head_sha,
        "draft": False,
    }


# ---------------------------------------------------------------------------
# GitHub — PR diff 가져오기
# ---------------------------------------------------------------------------


async def fetch_pr_diff(client: httpx.AsyncClient, pr_number: int) -> PRDiff:
    """PR 파일 목록 + patch 추출 (크기 제한 적용)

    github_client.py의 get_pr_diff() 로직 이식.
    """
    url = f"{GITHUB_API_BASE}/repos/{GH_REPO}/pulls/{pr_number}/files"
    response = await client.get(url, headers=GH_HEADERS, params={"per_page": 100})
    response.raise_for_status()
    raw_files: list[dict[str, Any]] = response.json()

    files: list[FileDiff] = []
    total_lines = 0
    truncated = False

    for f in raw_files:
        status = f.get("status", "modified")
        patch = f.get("patch")

        # 바이너리 / patch 없는 파일
        if status == "binary" or patch is None:
            files.append(FileDiff(
                path=f["filename"],
                patch=None,
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                status=status,
            ))
            continue

        # 파일당 라인 수 제한
        patch_lines = patch.count("\n")
        if patch_lines > MAX_FILE_LINES:
            lines = patch.split("\n")
            patch = (
                "\n".join(lines[:MAX_FILE_LINES])
                + f"\n... (파일이 너무 큼, {patch_lines}줄 → {MAX_FILE_LINES}줄로 잘림)"
            )
            patch_lines = MAX_FILE_LINES

        # 전체 라인 수 제한
        if total_lines + patch_lines > MAX_TOTAL_LINES:
            truncated = True
            remaining = MAX_TOTAL_LINES - total_lines
            if remaining > 0:
                lines = patch.split("\n")
                patch = "\n".join(lines[:remaining]) + "\n... (PR이 너무 큼, 잘림)"
                total_lines += remaining
            files.append(FileDiff(
                path=f["filename"],
                patch=patch,
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                status=status,
            ))
            break

        total_lines += patch_lines
        files.append(FileDiff(
            path=f["filename"],
            patch=patch,
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            status=status,
        ))

    return PRDiff(
        files=files,
        total_additions=sum(f.additions for f in files),
        total_deletions=sum(f.deletions for f in files),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Claude — 코드 리뷰
# ---------------------------------------------------------------------------


def _build_anthropic_client() -> AsyncAnthropic | None:
    """Dual-auth: ANTHROPIC_AUTH_TOKEN(Bearer) 또는 ANTHROPIC_API_KEY

    _DIRECT_ENDPOINT_MODE가 True이면 None을 반환 — 호출자가 httpx를 직접 사용한다.
    """
    if _DIRECT_ENDPOINT_MODE:
        if not (ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY):
            raise RuntimeError("ANTHROPIC_API_KEY 또는 ANTHROPIC_AUTH_TOKEN이 필요합니다.")
        logger.info("Direct endpoint mode: POST %s", ANTHROPIC_BASE_URL)
        return None

    kwargs: dict[str, Any] = {}
    if ANTHROPIC_BASE_URL:
        kwargs["base_url"] = ANTHROPIC_BASE_URL
    if ANTHROPIC_AUTH_TOKEN:
        # 내부 API — Bearer 토큰
        kwargs["api_key"] = "dummy"  # SDK 필수 파라미터 충족
        kwargs["default_headers"] = {"Authorization": f"Bearer {ANTHROPIC_AUTH_TOKEN}"}
    elif ANTHROPIC_API_KEY:
        kwargs["api_key"] = ANTHROPIC_API_KEY
    else:
        raise RuntimeError("ANTHROPIC_API_KEY 또는 ANTHROPIC_AUTH_TOKEN이 필요합니다.")
    return AsyncAnthropic(**kwargs)


def _build_user_message(diff: PRDiff, pr_title: str, pr_body: str) -> str:
    """Claude에게 보낼 사용자 메시지 생성 (review_service.py 재사용)"""
    parts: list[str] = []

    if pr_title:
        parts.append(f"## PR 제목\n{pr_title}")
    if pr_body:
        parts.append(f"## PR 설명\n{pr_body[:500]}")

    if diff.truncated:
        parts.append("> ⚠️ PR이 너무 커서 일부만 분석됩니다.")

    parts.append("## 변경된 파일 diff\n")

    for f in diff.files:
        if f.patch is None:
            parts.append(f"### {f.path} (바이너리/변경 없음)\n")
            continue
        parts.append(f"### {f.path}\n```diff\n{f.patch}\n```\n")

    return "\n".join(parts)


def _parse_json_response(raw: str) -> list[dict[str, Any]]:
    """3중 폴백 JSON 파싱 (review_service.py 재사용)"""
    # 1차: 직접 파싱
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # 2차: 코드블록 제거
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # 3차: json_repair
    try:
        from json_repair import repair_json  # type: ignore[import]
        repaired = repair_json(cleaned, return_objects=True)
        if isinstance(repaired, list):
            logger.warning("json_repair로 복구 성공")
            return repaired
    except Exception as e:
        logger.debug("json_repair 실패: %s", e)

    logger.error("JSON 파싱 최종 실패. raw(앞 200자)=%s", raw[:200])
    return []


def _validate_comments(
    raw: list[dict[str, Any]],
    valid_paths: set[str],
) -> list[ReviewComment]:
    """Claude 출력 검증 — path/line 범위 확인 (review_service.py 재사용)"""
    result: list[ReviewComment] = []

    for item in raw:
        path = item.get("path", "")
        if not path or path not in valid_paths:
            logger.debug("유효하지 않은 path 스킵: %s", path)
            continue

        line = item.get("line")
        side = item.get("side", "RIGHT")
        category = item.get("category", "style")
        severity = item.get("severity", "minor")
        body = item.get("body", "")

        if not body:
            continue

        if severity not in ("critical", "major", "minor", "info"):
            logger.warning("유효하지 않은 severity: %r → 'minor'로 정규화", severity)
            severity = "minor"
        if category not in ("bug", "security", "performance", "style", "test"):
            logger.warning("유효하지 않은 category: %r → 'style'로 정규화", category)
            category = "style"

        if line is not None and (not isinstance(line, int) or line < 1):
            line = None

        result.append(ReviewComment(
            path=path,
            line=line,
            side=side,
            category=category,
            severity=severity,
            body=body,
        ))

    return result


async def _call_claude_direct(system_prompt: str, user_message: str) -> str:
    """_DIRECT_ENDPOINT_MODE: ANTHROPIC_BASE_URL에 httpx 직접 POST."""
    token = ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)) as client:
                resp = await client.post(ANTHROPIC_BASE_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = "".join(
                    block.get("text", "") for block in data.get("content", [])
                    if block.get("type") == "text"
                )
                usage = data.get("usage", {})
                logger.info(
                    "Claude 응답 (direct): input_tokens=%d, output_tokens=%d",
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
                return text
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning("Direct API 오류 (시도 %d/3): %s — %ds 후 재시도", attempt, exc, wait)
            if attempt < 3:
                await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Direct API HTTP 오류: {exc.response.status_code} — {exc.response.text[:200]}") from exc

    raise RuntimeError(f"Direct API 3회 실패: {last_exc}") from last_exc


async def review_with_claude(
    diff: PRDiff,
    pr_title: str,
    pr_body: str,
) -> list[ReviewComment]:
    """Claude로 PR diff 분석 + 리뷰 코멘트 반환"""
    system_prompt = REVIEW_SYSTEM_PROMPT_KO if LANGUAGE == "ko" else REVIEW_SYSTEM_PROMPT_EN
    user_message = _build_user_message(diff, pr_title, pr_body)

    if _DIRECT_ENDPOINT_MODE:
        _build_anthropic_client()  # 인증 정보 검증만
        text = await _call_claude_direct(system_prompt, user_message)
    else:
        anthropic = _build_anthropic_client()

        # tenacity 없이 단순 retry (3회)
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await anthropic.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                break
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("Claude API 오류 (시도 %d/3): %s — %ds 후 재시도", attempt, exc, wait)
                if attempt < 3:
                    await asyncio.sleep(wait)
        else:
            raise RuntimeError(f"Claude API 3회 실패: {last_exc}") from last_exc

        text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )

        logger.info(
            "Claude 응답: input_tokens=%d, output_tokens=%d",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

    raw_comments = _parse_json_response(text)
    valid_paths = {f.path for f in diff.files}
    comments = _validate_comments(raw_comments, valid_paths)

    logger.info("리뷰 완료: 총 %d개 → 유효 %d개", len(raw_comments), len(comments))
    return comments


# ---------------------------------------------------------------------------
# GitHub — 리뷰 게시
# ---------------------------------------------------------------------------


def _format_comment_body(c: ReviewComment) -> str:
    """severity 이모지 + 카테고리 태그로 포맷 (github_client.py 재사용)"""
    emoji_map = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}
    category_label = {
        "bug": "버그",
        "security": "보안",
        "performance": "성능",
        "style": "스타일",
        "test": "테스트",
    }.get(c.category, c.category)
    emoji = emoji_map.get(c.severity, "⚪")
    return f"{emoji} **[{category_label}]** {c.body}"


def _generate_summary(comments: list[ReviewComment]) -> str:
    """리뷰 요약 생성 (github_client.py 재사용)"""
    if not comments:
        return "✅ 코드 리뷰 완료 — 특이사항 없음"

    critical = sum(1 for c in comments if c.severity == "critical")
    major = sum(1 for c in comments if c.severity == "major")
    minor = sum(1 for c in comments if c.severity == "minor")
    info = sum(1 for c in comments if c.severity == "info")

    lines = ["## 🤖 AI 코드 리뷰 결과\n"]
    if critical:
        lines.append(f"- 🔴 Critical: {critical}건")
    if major:
        lines.append(f"- 🟠 Major: {major}건")
    if minor:
        lines.append(f"- 🟡 Minor: {minor}건")
    if info:
        lines.append(f"- 🔵 Info: {info}건")

    lines.append(f"\n총 **{len(comments)}건** 검토되었습니다.")
    return "\n".join(lines)


async def post_review(
    client: httpx.AsyncClient,
    pr_number: int,
    head_sha: str,
    comments: list[ReviewComment],
) -> None:
    """GitHub PR에 리뷰 코멘트 게시 (github_client.py post_review() 이식)"""
    if not comments:
        # 이슈 없음 → LGTM 이슈 코멘트
        url = f"{GITHUB_API_BASE}/repos/{GH_REPO}/issues/{pr_number}/comments"
        payload = {"body": "✅ **AI 코드 리뷰 완료** — 특이사항이 없습니다. LGTM! 🎉"}
        resp = await client.post(url, headers=GH_HEADERS, json=payload)
        resp.raise_for_status()
        logger.info("LGTM 코멘트 게시 완료")
        return

    github_comments = [
        {
            "path": c.path,
            "body": _format_comment_body(c),
            "side": c.side,
            **({"line": c.line} if c.line is not None else {}),
        }
        for c in comments
    ]

    body_text = _generate_summary(comments)
    payload = {
        "commit_id": head_sha,
        "body": body_text,
        "event": "COMMENT",
        "comments": github_comments,
    }

    url = f"{GITHUB_API_BASE}/repos/{GH_REPO}/pulls/{pr_number}/reviews"
    try:
        resp = await client.post(url, headers=GH_HEADERS, json=payload)
        resp.raise_for_status()
        logger.info("리뷰 게시 완료 (id=%s)", resp.json().get("id"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 422:
            # 422: 코멘트 위치 오류 → line 없는 file-level comment로 재시도
            logger.warning("422 오류 — file-level comment로 폴백")
            fallback_comments = [
                {"path": c.path, "body": _format_comment_body(c), "side": c.side}
                for c in comments
            ]
            fallback_payload = {
                "commit_id": head_sha,
                "body": body_text,
                "event": "COMMENT",
                "comments": fallback_comments,
            }
            resp = await client.post(url, headers=GH_HEADERS, json=fallback_payload)
            resp.raise_for_status()
            logger.info("폴백 리뷰 게시 완료 (id=%s)", resp.json().get("id"))
        else:
            raise


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


async def main() -> None:
    if not GH_TOKEN:
        raise RuntimeError("GITHUB_TOKEN이 설정되지 않았습니다.")
    if not GH_REPO:
        raise RuntimeError("GITHUB_REPOSITORY가 설정되지 않았습니다.")

    pr = load_pr_info()
    logger.info("PR #%d 리뷰 시작: %s", pr["number"], pr["title"])

    if pr["draft"]:
        logger.info("Draft PR — 스킵합니다.")
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0)) as client:
        diff = await fetch_pr_diff(client, pr["number"])
        if not diff.files:
            logger.info("변경된 파일 없음 — 스킵합니다.")
            return

        logger.info("분석 대상: %d개 파일, truncated=%s", len(diff.files), diff.truncated)

        comments = await review_with_claude(diff, pr["title"], pr["body"])
        await post_review(client, pr["number"], pr["head_sha"], comments)

    logger.info("완료: %d개 코멘트 게시", len(comments))


if __name__ == "__main__":
    asyncio.run(main())
