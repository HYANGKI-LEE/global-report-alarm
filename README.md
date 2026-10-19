# global-report-alarm

국제기구·연준 등 사용자가 고른 페이지에 새 리포트가 올라오면 텔레그램 채널로 알려주는 봇.

- 대상 목록: `sources.yaml`
- 실행: GitHub Actions(`.github/workflows/poll.yml`) — 한국시간 08:00 / 13:00 / 18:00, Actions 탭에서 수동 실행 가능
- 이미 보낸 항목 기록: `state/seen.json` (자동 커밋)
- 새로 추가된 페이지는 첫 실행 때 기존 글을 '본 것'으로만 기록하고, 그 이후 올라오는 글부터 알림
- 같은 페이지가 3회 연속 수집 실패하면 경고 메시지 1회 발송

필요한 GitHub Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

로컬 시험(전송·저장 없이 출력만): `DRY_RUN=1 python main.py`
