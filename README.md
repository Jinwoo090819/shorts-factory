# shorts-factory

0원 지출을 목표로 한 YouTube Shorts 자동화 프로젝트입니다.

목표: 매일 한국시간 19:00에 잡지식 Shorts 1개를 자동 공개합니다.

## 현재 파이프라인

1. ChatGPT Automation이 매일 05:00 KST에 팩트 검증된 한국어 잡지식 대본 1개를 `data/scripts/YYYY-MM-DD.json`에 저장합니다.
2. GitHub Actions가 매일 16:30 KST에 `status=ready`인 가장 오래된 대본 1개를 선택합니다.
3. Edge TTS가 한국어 음성을 생성합니다.
4. Pexels API에서 세로 B-roll 영상을 가져옵니다.
5. FFmpeg가 1080x1920 영상, 음성, 자막을 합성합니다.
6. GitHub Pages가 완성된 `short.mp4`를 Buffer가 읽을 수 있는 공개 HTTPS 주소로 잠시 제공합니다.
7. Buffer API가 연결된 YouTube 채널에 영상을 19:00 KST로 예약합니다.
8. 예약이 성공한 대본은 `scheduled`로 바뀌고 `data/history.json`에 기록됩니다.

## 필요한 GitHub Secrets

- `PEXELS_API_KEY`
- `BUFFER_API_KEY`

Google/YouTube OAuth Client ID, Client Secret, Refresh Token과 Gemini API는 사용하지 않습니다.

비밀키는 코드나 채팅에 붙여넣지 말고 GitHub Repository Settings > Secrets and variables > Actions에 저장하세요.

## 채널 교체

Buffer에 YouTube 채널이 하나만 연결되어 있으면 자동으로 그 채널을 사용합니다.

여러 YouTube 채널이 연결되어 있을 때는 GitHub Repository Variable `BUFFER_CHANNEL_ID`를 설정하면 해당 채널만 사용합니다. 따라서 나중에 대상 채널이 바뀌어도 영상 제작 코드는 그대로 두고 Buffer 연결과 이 변수만 바꾸면 됩니다.

## GitHub Pages 1회 설정

Repository Settings > Pages > Build and deployment > Source를 `GitHub Actions`로 설정해야 합니다.

영상 파일은 Git 기록에 커밋하지 않고 Pages deployment artifact로만 배포하므로 저장소가 영상 파일 때문에 계속 커지는 구조가 아닙니다.

## 실행 시간

- 05:00 KST: ChatGPT가 대본 1개 저장
- 16:30 KST: GitHub Actions가 영상 제작, Pages 임시 배포, Buffer 예약
- 19:00 KST: Buffer가 YouTube에 자동 게시

수동 실행도 지원합니다.

## 현재 기본값

- 대본 시작 문구: `그거 아세요?`
- 대본 길이: 약 20~30초
- TTS 음성: `ko-KR-SunHiNeural`
- 영상: Pexels portrait video
- 출력: 1080x1920 H.264/AAC
- YouTube 카테고리: Education (27)
- Made for Kids: false
- AI-assisted/AI-generated metadata: true

## 안정성 설계

- Buffer 예약 생성이 성공해야만 대본 상태를 `scheduled`로 변경합니다.
- 영상 공개 URL이 실제로 열리는지 확인한 뒤 Buffer에 예약합니다.
- 실패 시 대본은 `ready` 상태로 남아 다음 실행에서 다시 시도할 수 있습니다.
- Buffer에 연결된 YouTube 채널이 0개이거나 여러 개인데 대상 변수가 없으면 잘못된 채널에 게시하지 않고 실행을 중단합니다.

## 주의

- 외부 무료 티어와 서비스 정책은 바뀔 수 있으므로 0원 운영이 영구 보장되는 것은 아닙니다.
- GitHub Pages와 Buffer 등 외부 서비스 장애가 발생하면 해당 실행이 실패할 수 있습니다.
- 자동 생성 영상은 사실 확인과 품질 검토가 중요합니다.
- 자동 업로드가 YouTube 수익화 승인을 보장하지는 않습니다.
