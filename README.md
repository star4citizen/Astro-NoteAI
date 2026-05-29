# Astro-Note AI

Astro-Note AI는 논문 PDF를 업로드하면 로컬 연구 Wiki를 만들어 주는 데스크톱 앱입니다.
PDF 내용을 읽어 Wiki 문서, 한글 요약, 의미 기반 주제, 그래프 연결을 만들고 Obsidian으로
가져갈 수 있는 ZIP export를 지원합니다.

## 주요 기능

- 여러 PDF 논문 업로드
- 논문 텍스트 기반 Wiki 자동 생성
- 한글 요약 생성
- 앱 안에서 PDF, Wiki, Chat, Graph 통합 탐색
- Ollama 또는 OpenAI-compatible API 선택 지원
- Markdown 문서 Wiki 업로드
- TeX 수식 MathJax 렌더링
- Obsidian vault ZIP export
- API key와 개인 설정은 사용자 PC에만 저장

## 저장소 구조

```text
_internal/app/config/   앱 설정과 프롬프트
_internal/app/scripts/  실행 및 처리 스크립트
_internal/app/src/      앱 Python 모듈
_internal/app/ui/       프론트엔드 파일
RUNTIME_REQUIREMENTS.md Linux 런타임 요구 사항
```

패키징된 실행 파일, PyInstaller 런타임, 업로드된 PDF, 생성된 Wiki/Graph/cache/log 파일은
Git 저장소에 포함하지 않습니다. 배포용 `.exe`, `.dmg`, `.zip` 파일은 GitHub Releases에
업로드하는 방식을 권장합니다.

## 실행 전 설정

앱의 `Settings`에서 LLM provider를 선택합니다.

Ollama를 사용할 경우:

```bash
ollama serve
ollama pull <model-name>
```

OpenAI-compatible 서버를 사용할 경우 필요한 값:

```text
API base URL
API key
```

예:

```text
http://localhost:8000/v1
```

## 데이터 위치

앱 내부 생성 데이터는 다음 경로에 만들어집니다.

```text
data/raw/papers/       업로드한 PDF
data/markdown/         추출된 Markdown
data/text/             추출된 텍스트
data/summaries/        한글 요약
wiki/papers/           논문 Wiki
wiki/document/         Markdown 문서 Wiki
graphify-out/          그래프 뷰어
```

사용자별 LLM 설정은 OS별 설정 폴더에 저장됩니다.

```text
Linux:   ~/.config/AstroPhLLMWiki/local_settings.json
macOS:   ~/Library/Application Support/AstroPhLLMWiki/local_settings.json
Windows: %APPDATA%\AstroPhLLMWiki\local_settings.json
```

## 주의

공개 저장소에 올리기 전에는 테스트 PDF, 생성된 요약/Wiki, API key, 개인 자료가 포함되지
않았는지 확인해야 합니다. 이 저장소 설정은 해당 생성 데이터를 기본적으로 제외합니다.
