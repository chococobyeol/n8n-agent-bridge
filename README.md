# Cursor ACP Multi-session Bridge

Cursor Agent ACP 를 Discord / n8n 과 연결하는 브리지.

- 멀티세션 지원
- 취소 / 강제종료 지원
- 권한 요청 자동 거절
- 세션별 작업 분리

## 기능

- 멀티 세션
- `/sessions`
- `/new NAME`
- `/close NAME`
- `/closeall`
- `/cancel NAME`
- `/kill NAME`

세션 지정:

```
debug: 에러 원인 분석해줘
docs: README 작성해줘
main: 파일 만들어줘
```

## 설치

Python 3.10+

```bash
pip install requests
```

또는

```bash
pip install -r requirements.txt
```

## 설정

```bash
cp config.example.json config.json
```

`config.json` 수정:

- `workspace`
- `n8n_base_url`
- `shared_secret`

## 실행

```bash
python bridge_multisession.py
```

## 보안 주의

- `config.json` 은 커밋하지 말 것
- `shared_secret` / webhook / token 절대 공개하지 말 것
- `logs/` 는 커밋하지 말 것
