# global-report-alarm

국제기구·연준 등 사용자가 고른 페이지에 새 리포트가 올라오면 텔레그램 채널로 알려주는 봇.

- 대상 목록: `sources.yaml`
- 실행: GitHub Actions(`.github/workflows/poll.yml`) — 30분마다(매시 7분·37분), Actions 탭에서 수동 실행 가능
- 이미 보낸 항목 기록: `state/seen.json` (자동 커밋)
- 새로 추가된 페이지는 첫 실행 때 기존 글을 '본 것'으로만 기록하고, 그 이후 올라오는 글부터 알림
- 발간일을 알 수 있는 글은 최근 3일 이내만 알림 (`MAX_AGE_DAYS`, 대상별 `max_age_days`)
- 같은 페이지가 3회 연속 수집 실패하면 경고 메시지 1회 발송

필요한 GitHub Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`

- 실제 브라우저로 여는 사이트: `sources.yaml` 의 `browser_hosts` (캔자스시티 연준)
- 메일 중계: IMF·세인트루이스 연준은 클라우드 서버 접속을 막아, 공식 이메일 알림을 봇 전용 Gmail 로 받아 전달

로컬 시험(전송·저장 없이 출력만): `DRY_RUN=1 python main.py`

## 한국어 번역
- 리포트 알림의 초록 3줄을 DeepL(API Free, 월 50만 자 무료)로 한국어 번역해서 보냄 — GitHub Secrets `DEEPL_API_KEY`
- 키가 없거나 월 한도를 넘으면 영어 원문으로 보냄
- (보관) Claude Code 예약 작업으로 한국어 요약을 붙이는 방식은 `ROUTINE_SUMMARY = False` 로 꺼둠
