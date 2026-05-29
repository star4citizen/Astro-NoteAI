# Astro-Note AI

Astro-Note AI는 논문 PDF를 업로드하면 로컬 연구 Wiki를 만들어 주는 데스크톱 앱입니다.
PDF 내용을 읽어 Wiki 문서, 한글 요약, 의미 기반 주제, 그래프 연결을 만들고 Obsidian으로
가져갈 수 있는 ZIP export를 지원합니다.

## 왜 LLM Wiki가 필요한가

논문을 많이 읽을수록 문제는 단순한 "요약"이 아니라 지식의 재사용입니다. PDF는 파일 단위로
흩어지고, 메모는 작성한 순간의 맥락에 묶이며, 나중에 다시 찾을 때는 제목이나 키워드만으로는
충분하지 않은 경우가 많습니다. LLM Wiki는 논문을 한 번 읽고 끝내는 자료가 아니라 계속 검색하고
연결하고 확장할 수 있는 연구 지식베이스로 바꾸는 방식입니다.

LLM Wiki가 주는 장점은 다음과 같습니다.

- 논문 PDF를 Wiki 문서, 한글 요약, 주제 태그, 그래프 관계로 변환해 다시 찾기 쉽게 만듭니다.
- 여러 논문의 공통 주제, 방법론, 천체/관측 대상, 데이터셋을 연결해 연구 흐름을 볼 수 있습니다.
- Chat에서 연구 질문을 던지면 저장된 Wiki와 논문 내용을 근거로 답을 찾을 수 있습니다.
- 답변을 다시 Wiki 업데이트 제안으로 저장해 읽기, 질문, 정리 과정을 하나의 루프로 만들 수 있습니다.
- Obsidian export를 통해 앱 밖에서도 Markdown 기반 연구 노트를 이어서 관리할 수 있습니다.
- API key와 개인 설정은 사용자 PC에 저장되며, 업로드한 논문과 생성 데이터는 기본적으로 Git에 포함하지 않습니다.

## 주요 기능

- 여러 PDF 논문 또는 Markdown 문서 업로드
- 논문 텍스트 기반 Wiki 자동 생성
- 한글 요약 자동 생성 및 재생성
- 앱 안에서 PDF, Wiki, Chat, Graph 통합 탐색
- ADS/arXiv/local 검색을 통한 논문 발견 및 Wiki 생성
- Ollama, OpenAI API, Gemini, Claude, OpenRouter, Groq, DeepSeek, xAI, Mistral, OpenAI-compatible API 지원
- TeX 수식 MathJax 렌더링
- Chat Q&A 저장 및 Wiki 업데이트 제안 생성
- Obsidian vault ZIP export
- API key와 개인 설정 로컬 저장

## 메뉴별 기능

### Dashboard

Dashboard는 연구 Wiki의 현재 상태를 빠르게 보는 시작 화면입니다.

- 전체 논문 수, 그래프에 연결된 논문 수, Wiki 페이지 수, 그래프 edge 수를 지표로 보여줍니다.
- Semantic Topics에서 앱이 추출한 의미 기반 주제 묶음을 확인할 수 있습니다.
- Recent Papers에서 최근 처리된 논문을 바로 열어 PDF, Wiki, Chat으로 이동할 수 있습니다.
- 새 업로드나 Wiki 생성 후 Refresh로 현재 연구 노트 상태를 다시 불러올 수 있습니다.

### Upload

Upload는 논문과 문서를 연구 Wiki로 변환하는 작업 공간입니다.

- `Upload Paper / Document`에서 PDF, Markdown, plain text 파일을 여러 개 선택해 처리할 수 있습니다.
- 업로드된 PDF는 텍스트 추출, Markdown 변환, Wiki 생성, 한글 요약 생성 과정을 거칩니다.
- `Batch Process`는 폴더 단위로 여러 PDF를 처리합니다. 새 파일/변경 파일만 처리하거나 전체 재처리를 선택할 수 있습니다.
- `Work Prompt`에서 업로드 처리에 쓰이는 작업 프롬프트를 확인, 수정, 저장, 초기화할 수 있습니다.
- 진행률 패널에서 현재 단계, 처리 중인 파일, 로그, 성공/실패 상태를 볼 수 있습니다.
- `Discover Papers`에서 연구 목표, 방법론, 천체명, arXiv syntax 등으로 ADS/arXiv/local 검색을 수행할 수 있습니다.
- `LLM goal` 옵션은 사용자의 연구 목표를 천문학 검색어로 변환해 더 적합한 후보를 찾는 데 사용됩니다.
- 검색 결과는 유사도 그래프와 후보 목록으로 표시되며, 선택한 논문은 다운로드 후 `Build Wiki`로 바로 Wiki화할 수 있습니다.

### Papers

Papers는 처리된 논문을 검토하고 읽는 메뉴입니다.

- 제목, 논문 ID, 초록 기반 검색을 지원합니다.
- 상태 필터로 처리 완료, 그래프 연결, 실패, 삭제 대상 등 논문 상태별로 목록을 볼 수 있습니다.
- 논문 상세 화면에서 제목, arXiv ID, 카테고리, 주제, relevance score, classification rationale을 확인할 수 있습니다.
- 내장 PDF 뷰어에서 페이지 이동, 확대/축소, 폭 맞춤으로 논문을 읽을 수 있습니다.
- `Info`를 누르면 한글 요약, classification, abstract, 생성된 Wiki 미리보기를 확인할 수 있습니다.
- `다시 생성`으로 한글 요약을 재생성할 수 있습니다.
- `Wiki` 버튼으로 해당 논문의 Wiki 페이지로 이동하고, `Chat` 버튼으로 해당 논문만을 문맥으로 질문할 수 있습니다.
- 필요 없는 논문은 Delete로 PDF, 텍스트, 요약, 그래프 링크, Wiki 참조를 정리할 수 있습니다.

### Wiki

Wiki는 앱이 만든 연구 노트를 읽고 관리하는 중심 메뉴입니다.

- `wiki/papers`, `wiki/document`, `wiki/daily`, `wiki/topics` 등 폴더 구조로 Markdown Wiki를 탐색합니다.
- 논문 Wiki는 핵심 내용, 근거 섹션, 관련 주제, 링크를 HTML로 렌더링해 보여줍니다.
- 문서 안의 Wiki 링크를 클릭하면 연결된 페이지를 열 수 있고, 논문 소스 링크는 보조 reader에서 미리볼 수 있습니다.
- TeX 수식은 MathJax로 렌더링됩니다.
- Chat에서 생성한 Wiki update proposal은 Wiki 안에서 검토하고 `Apply Proposal`로 대상 페이지에 반영할 수 있습니다.
- Paper Wiki에서 Delete를 실행하면 관련 PDF, 추출 텍스트, 요약, 그래프 연결, Wiki 참조까지 함께 정리됩니다.
- `Choose Folder`와 `Export ZIP`으로 현재 Wiki를 Obsidian vault용 ZIP으로 내보낼 수 있습니다.

### Chat

Chat은 저장된 연구 Wiki와 논문 내용을 대상으로 질문하는 메뉴입니다.

- 일반 Wiki Chat에서는 전체 Wiki를 대상으로 연구 질문을 할 수 있습니다.
- Papers나 Dashboard에서 특정 논문 Chat을 열면 해당 논문 PDF/Wiki를 문맥으로 질문할 수 있습니다.
- 선택한 Chat model로 답변을 생성하며, 답변 안의 Wiki 링크와 수식도 앱 안에서 읽을 수 있습니다.
- 특정 논문 Chat에서는 PDF pane과 한글 요약을 함께 보며 질문할 수 있습니다.
- 답변 아래의 `Save Q&A`로 유용한 질의응답을 저장할 수 있습니다.
- `Propose Wiki Update`는 답변과 근거를 바탕으로 Wiki 업데이트 제안을 만들어 Review/Wiki 흐름으로 넘깁니다.

### Graph

Graph는 논문과 Wiki 주제 사이의 관계를 시각적으로 보는 메뉴입니다.

- 생성된 `graphify-out` 결과를 iframe으로 표시합니다.
- 논문, 주제, 문서 사이의 연결 관계를 확인해 연구 흐름과 관련 논문 묶음을 파악할 수 있습니다.
- Upload의 Discover Papers 그래프는 후보 논문 사이의 유사도를 보여주고, Graph 메뉴는 로컬 Wiki 전체의 연결망을 확인하는 용도입니다.

### Settings

Settings는 LLM provider와 모델 연결을 관리하는 메뉴입니다.

- Ollama 또는 API provider를 선택할 수 있습니다.
- OpenAI API, Google Gemini, Anthropic Claude, OpenRouter, Groq, DeepSeek, xAI, Mistral, custom OpenAI-compatible endpoint를 지원합니다.
- API base URL, API key, NASA ADS API key, Ollama base URL을 설정할 수 있습니다.
- Chat model과 Retrieval model을 선택하거나 직접 입력할 수 있습니다.
- 저장 전 LLM 연결 테스트를 수행하며, 테스트가 성공해야 설정이 저장됩니다.
- 설정은 OS별 사용자 설정 폴더에 저장되어 Git 저장소에 포함되지 않습니다.

### Review / Runs

일반 메뉴 탭에는 보이지 않지만 앱 내부에는 관리용 화면도 포함되어 있습니다.

- Review Queue는 Chat에서 만든 Wiki update proposal을 검토하고 반영하는 데 사용됩니다.
- Wiki Lint는 Wiki 문서의 숫자, 근거, 형식 문제를 점검하는 보조 도구입니다.
- Runs 화면은 classify, extract, ingest, digest, newsletter, curate, graph, semantic wiki, QMD index, Korean summaries, lint 같은 agent 작업을 직접 실행하는 고급 도구입니다.

## 다운로드 및 실행

이 저장소에는 Windows/macOS 패키지 파일이 Git LFS로 포함되어 있습니다.

```text
Astro-Note-AI.exe   Windows 실행 파일
Astro-Note-AI.dmg   macOS 디스크 이미지
```

Linux one-folder 패키지는 실행 파일과 `_internal` 런타임 폴더가 함께 필요합니다. 전체 런타임 폴더는
매우 크기 때문에 Git 저장소에는 포함하지 않고, 배포가 필요할 때 GitHub Releases에 올리는 방식을
권장합니다.

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

## 저장소 구조

```text
_internal/app/config/   앱 설정과 프롬프트
_internal/app/scripts/  실행 및 처리 스크립트
_internal/app/src/      앱 Python 모듈
_internal/app/ui/       프론트엔드 파일
RUNTIME_REQUIREMENTS.md Linux 런타임 요구 사항
```

Windows/macOS 패키지 파일은 Git LFS로 관리합니다. PyInstaller 런타임 폴더,
업로드된 PDF, 생성된 Wiki/Graph/cache/log 파일은 Git 저장소에 포함하지 않습니다.

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
