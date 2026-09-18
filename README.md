# shorts-factory

0원 기준 YouTube Shorts 자동화 프로젝트입니다.

목표: 매일 한국시간 19:00에 잡지식 Shorts 1개를 자동 공개합니다.

파이프라인:

1. 한국어 Wikipedia 공개 문서에서 후보 주제를 가져옵니다.
2. Gemini API가 제공된 출처 문장만 바탕으로 한 가지 사실을 20~30초 대본으로 재구성합니다.
3. MeloTTS가 한국어 음성을 로컬에서 생성합니다.
4. Pexels API에서 세로 B-roll 영상을 가져옵니다.
5. FFmpeg가 1080x1920 영상, 음성, 자막을 합성합니다.
6. YouTube Data API가 비공개 업로드 후 19:00 공개 예약을 설정합니다.
7. 사용한 출처를 `data/history.json`에 기록해 중복을 줄입니다.

## 필요한 GitHub Secrets

- `GEMINI_API_KEY`
- `PEXELS_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

비밀키는 코드나 채팅에 붙여넣지 말고 GitHub Repository Settings > Secrets and variables > Actions에 저장하세요.

## 실행 시간

GitHub Actions는 매일 17:30 Asia/Seoul에 제작을 시작하고, 완성된 영상을 19:00 공개 예약합니다. 수동 실행도 지원합니다.

## 주의

- YouTube Data API에서 신규/미감사 API 프로젝트로 업로드한 영상은 비공개로 제한될 수 있습니다. 이 경우 Google의 API 프로젝트 감사 절차가 완료되어야 자동 공개가 가능합니다.
- 자동 생성 영상은 사실 확인과 품질 검토가 중요합니다. 이 프로젝트는 Wikipedia 원문에 포함된 정보만 대본의 사실 근거로 사용하도록 제한합니다.
- 자동 업로드가 수익화 승인을 보장하지는 않습니다.
