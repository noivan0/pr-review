# 🤖 AI PR Review

![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-automated-2088FF?logo=github-actions&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Anthropic Claude](https://img.shields.io/badge/Claude-claude--sonnet--4--6-D4421E?logo=anthropic&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

> PR이 열릴 때마다 Claude AI가 자동으로 코드를 리뷰합니다.
> 서버 불필요, 인프라 비용 0원, GitHub Actions만으로 동작.

---

## ✨ 기능

- **자동 트리거** — PR `opened` / `synchronize` / `reopened` 이벤트 감지
- **6가지 체크리스트** — 버그·보안(OWASP Top 10)·성능(N+1)·코드품질·에러핸들링·테스트
- **심각도 분류** — 🔴 Critical / 🟠 Major / 🟡 Minor / 🔵 Info 인라인 코멘트
- **한국어/영어** — `REVIEW_LANGUAGE` 환경변수로 전환
- **크기 제한** — 파일당 500줄, PR 전체 3,000줄 자동 트런케이션
- **Draft PR 스킵** — Draft 상태 PR은 자동 건너뜀
- **이중 인증** — 공개 API(`ANTHROPIC_API_KEY`) 또는 사내 Bearer 토큰(`ANTHROPIC_AUTH_TOKEN`) 지원

---

## ⚡ 빠른 시작 — 다른 레포에 적용하기

### 방법 1: curl로 파일 복사 (권장)

```bash
# 1. 워크플로우 & 스크립트 복사
mkdir -p .github/workflows scripts

curl -sSL https://raw.githubusercontent.com/noivan0/pr-review/main/.github/workflows/pr-review.yml \
  -o .github/workflows/pr-review.yml

curl -sSL https://raw.githubusercontent.com/noivan0/pr-review/main/scripts/pr_reviewer.py \
  -o scripts/pr_reviewer.py

# 2. 의존성 파일 생성 (anthropic + httpx)
curl -sSL https://raw.githubusercontent.com/noivan0/pr-review/main/pyproject.toml \
  -o pyproject.toml
```

### 방법 2: git clone 후 복사

```bash
git clone https://github.com/noivan0/pr-review.git
cp pr-review/.github/workflows/pr-review.yml .github/workflows/
cp pr-review/scripts/pr_reviewer.py scripts/
cp pr-review/pyproject.toml .
```

### 방법 3: 이 레포를 Fork

GitHub에서 **Fork** 버튼 클릭 → Secrets 설정 → PR 생성하면 바로 동작.

---

## 🔑 GitHub Secrets 설정

레포 → **Settings → Secrets and variables → Actions → New repository secret**

### 공개 Anthropic API 사용 시

| Secret 이름 | 값 | 필수 |
|---|---|:---:|
| `ANTHROPIC_API_KEY` | `sk-ant-...` | ✅ |

### 사내 API (self-hosted runner) 사용 시

| Secret 이름 | 값 | 필수 |
|---|---|:---:|
| `ANTHROPIC_AUTH_TOKEN` | Bearer 토큰 | ✅ |
| `ANTHROPIC_BASE_URL` | 사내 API 엔드포인트 URL | ✅ |

> `GITHUB_TOKEN`은 Actions가 자동으로 제공하므로 별도 설정 불필요.

---

## 📦 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `GITHUB_TOKEN` | Actions 자동 제공 | 자동 |
| `ANTHROPIC_API_KEY` | Claude API 키 | — |
| `ANTHROPIC_AUTH_TOKEN` | 사내 Bearer 토큰 (대안) | — |
| `ANTHROPIC_BASE_URL` | 사내 API URL | — |
| `REVIEW_LANGUAGE` | `ko` \| `en` | `ko` |
| `MAX_FILE_LINES` | 파일당 최대 diff 줄 수 | `500` |
| `MAX_TOTAL_LINES` | PR 전체 최대 diff 줄 수 | `3000` |
| `DEFAULT_MODEL` | Claude 모델 ID | `claude-sonnet-4-6` |
| `MAX_TOKENS` | 최대 응답 토큰 수 | `4096` |

---

## 🏗️ 아키텍처

```
PR 오픈/업데이트
    │
    ▼ GitHub Actions 트리거
    │
    ├─ GITHUB_EVENT_PATH 읽기 → PR 메타데이터 추출
    ├─ GET /repos/{owner}/{repo}/pulls/{n}/files → diff 수집
    │       파일당 500줄 · 전체 3,000줄 제한
    │
    ├─ Claude API 호출 (claude-sonnet-4-6)
    │       시스템 프롬프트: 6가지 체크리스트
    │       출력: JSON 배열 (path, line, severity, category, body)
    │
    └─ POST /repos/{owner}/{repo}/pulls/{n}/reviews
            🔴🟠🟡🔵 이모지 인라인 코멘트 게시
            (422 오류 시 file-level comment로 자동 폴백)
```

---

## 🖥️ Self-hosted Runner (사내망 API)

사내 네트워크의 Claude API를 사용할 경우 self-hosted runner를 사용하세요.

```yaml
# .github/workflows/pr-review.yml
jobs:
  review:
    runs-on: self-hosted   # ubuntu-latest → self-hosted
```

Self-hosted runner 설치는 [GitHub 공식 문서](https://docs.github.com/en/actions/hosting-your-own-runners)를 참고하세요.

---

## 🧪 로컬 테스트

```bash
# 의존성 설치
pip install uv && uv sync

# 실제 PR 테스트 (해당 PR에 실제 코멘트 게시됨 — 테스트 레포 사용 권장)
GITHUB_TOKEN=ghp_xxx \
GITHUB_REPOSITORY=your-org/test-repo \
PR_NUMBER=1 \
PR_TITLE="feat: add feature" \
HEAD_SHA=abc123 \
ANTHROPIC_API_KEY=sk-ant-xxx \
uv run python scripts/pr_reviewer.py
```

---

## 📁 프로젝트 구조

```
pr-review/
├── .github/
│   └── workflows/
│       └── pr-review.yml      # GitHub Actions 워크플로우
├── scripts/
│   └── pr_reviewer.py         # 메인 스크립트 (독립 실행형)
├── pyproject.toml             # 최소 의존성 (anthropic, httpx)
└── README.md
```

---

## 리뷰 예시

PR에 자동으로 게시되는 코멘트 형태:

**요약 (PR 본문)**
```
## 🤖 AI 코드 리뷰 결과

- 🔴 Critical: 1건
- 🟠 Major: 2건
- 🟡 Minor: 3건

총 6건 검토되었습니다.
```

**인라인 코멘트**
```
🔴 [보안] **문제**: SQL 인젝션 취약점

입력값을 직접 쿼리에 삽입하고 있습니다.

**수정 방법:**
```python
# Before
query = f'SELECT * FROM users WHERE id = {user_id}'
# After
query = 'SELECT * FROM users WHERE id = %s'
cursor.execute(query, (user_id,))
```
```

---

## License

MIT © [noivan0](https://github.com/noivan0)
