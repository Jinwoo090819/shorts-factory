# shorts-factory

0원 지출을 목표로 한 YouTube Shorts 자동화 프로젝트입니다.

목표: 매일 한국시간 19:00에 잡지식 Shorts 1개를 자동 공개합니다.

파이프라인:

1. ChatGPT Automation이 매일 05:00 KST에 팩트 검증된 한국어 잡지식 대본 1개를 `data/scripts/YYYY-MM-DD.json`에 저장합니다.
2. GitHub Actions가 매일 16:30 KST에 `status=ready`인 가장 오래된 대본 1개를 선택합니다.
3. Edge TTS가 한국어 음성을 생성합니다.
4. Pexels API에서 세로 B-roll 영상을 가져옵니다.
5. FFmpeg가 1080x1920 영상, 음성, 자막을 합성합니다.
6. YouTube Data API가 비공개 업로드 후 19:00 KST 공개 예약을 설정합니다.
7. 성공한 대본은 `published`로 바뀌고 `data/history.json`에 기록됩니다.

## 필요한 GitHub Secrets

- `PEXELS_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

Gemini API는 사용하지 않습니다. 대본은 ChatGPT Automation이 저장합니다.

비밀키는 코드나 채팅에 붙여넣지 말고 GitHub Repository Settings > Secrets and variables > Actions에 저장하세요.

## 실행 시간

- 05:00 KST: ChatGPT가 대본 1개 저장
- 16:30 KST: GitHub Actions가 영상 제작 및 YouTube 예약 업로드
- 19:00 KST: YouTube 공개

수동 실행도 지원합니다.

## 현재 기본값

- 대본 시작 문구: `그거 아세요?`
- 대본 길이: 약 20~30초
- TTS 음성: `ko-KR-SunHiNeural`
- 영상: Pexels portrait video
- 출력: 1080x1920 H.264/AAC

## 주의

- 외부 무료 티어/무료 서비스의 정책과 한도는 바뀔 수 있으므로 0원 운영이 영구 보장되는 것은 아닙니다.
- YouTube Data API에서 신규/미감사 API 프로젝트로 업로드한 영상은 비공개로 제한될 수 있습니다. 이 경우 Google의 API 프로젝트 감사 절차가 완료되어야 자동 공개가 가능합니다.
- 자동 생성 영상은 사실 확인과 품질 검토가 중요합니다.
- 자동 업로드가 YouTube 수익화 승인을 보장하지는 않습니다.
