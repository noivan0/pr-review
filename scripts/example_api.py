"""
example_api.py — 테스트용 샘플 코드 (의도적 이슈 포함)
실제 사용 코드 아님 — PR 리뷰 봇 동작 검증 전용
"""
import sqlite3
import os


def get_user(user_id):
    # BUG: SQL Injection 취약점
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchone()


def process_items(items):
    # BUG: 빈 리스트 처리 안 됨 (ZeroDivisionError)
    total = sum(items)
    average = total / len(items)
    return average


def read_secret():
    # BUG: 하드코딩된 API 키
    api_key = "sk-1234567890abcdef"
    return api_key


def login(username, password):
    # BUG: 비밀번호 평문 비교, 에러 핸들링 없음
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    result = cursor.execute(
        f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
    )
    return result.fetchone() is not None
# trigger: 2026년 03월 27일 금 오후 12:17:14
# retrigger 2026년 03월 27일 금 오후 12:21:27
