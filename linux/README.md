# Steam Deck / SteamOS(리눅스) 설치 도구

Windows 한글패치 런처(`ZeroParades_KR_Patch_v1.0.exe`)는 WebView2 화면을 쓰기 때문에 Steam Deck 같은 리눅스 환경에서는 띄우기 어렵습니다. `zp_kr_linux.py`는 런처의 설치 엔진을 파이썬으로 옮긴 명령줄 도구입니다.

- 런처와 같은 순서·같은 방법으로, 내 PC의 Steam 정품 원본에서 한글 파일을 만듭니다.
- 결과 5개의 SHA-256이 런처 manifest의 값과 같을 때만 바꿉니다. 따라서 결과는 Windows 런처로 설치한 것과 **바이트 단위로 같습니다**.
- 패치 자료(manifest·한국어 표·글꼴)는 공식 릴리스의 런처 exe 안에서 꺼내 씁니다. exe는 **실행하지 않고**, SHA-256을 확인한 뒤 내용만 읽습니다. 이 저장소에는 게임 파일이나 패치 결과물이 들어가지 않습니다.
- 원본 백업 위치(`게임 폴더/KR-AOS_한글패치_백업/ZeroParades/`)와 `receipt.json` 형식은 런처와 같습니다.

## 준비물
- 파이썬 3.8 이상, liblz4 (SteamOS에는 둘 다 기본으로 들어 있습니다)
- 지원 게임 버전: 1.1.40910 (Steam 빌드 26138821), v1.0 런처와 같습니다.

## 설치
1. Steam Deck을 **데스크톱 모드**로 전환하고 **Konsole**(터미널)을 엽니다.
2. 게임을 완전히 종료합니다. Steam은 켜 둡니다.
3. 아래를 실행합니다.
   ```sh
   curl -LO https://raw.githubusercontent.com/magicianaos/KR-AOS-ZeroParades/main/linux/zp_kr_linux.py
   python3 zp_kr_linux.py install
   ```
   공식 런처 exe(약 18MB)를 GitHub 릴리스에서 받아 `~/.cache/kr-aos-zeroparades/`에 둡니다. 이미 받아 둔 exe가 있으면 `--exe <경로>`로 지정합니다.
4. 게임 실행 후 **설정 → 게임플레이 → 텍스트 언어**에서 **한국어**(중국어 간체 자리)를 선택합니다.

## 명령과 옵션
| 명령 | 하는 일 |
|---|---|
| `status` | 대상 파일 5개·카탈로그·원본 백업 상태 보기 |
| `check` | 게임 파일은 그대로 두고, 임시 폴더에서 만들어 결과 해시만 미리 확인 |
| `install` | 설치: 원본 백업 → 백업본에서 만들기 → 결과 해시 확인 → 교체. 중간에 실패하면 원본으로 자동 복원 |
| `restore` | 백업한 원본으로 되돌리기 |

| 옵션 | 설명 |
|---|---|
| `--game <폴더>` | 게임 폴더 직접 지정 (기본: Steam 라이브러리에서 자동으로 찾음, SD 카드 라이브러리 포함) |
| `--steam-root <폴더>` | Steam 폴더 직접 지정 (예: Flatpak Steam은 `~/.var/app/com.valvesoftware.Steam/.local/share/Steam`) |
| `--exe <파일>` | 미리 받아 둔 공식 런처 exe |

## 되돌리기
`python3 zp_kr_linux.py restore`를 실행하거나 Steam의 **게임 파일 무결성 검사**를 하세요.

## Windows 런처와 다른 점: 정품 확인
런처의 정품 확인 가운데 리눅스에서 그대로 할 수 있는 것은 똑같이 합니다.
- 게임 안 Steam 모듈 파일(`steam_api64.dll`·`ZeroParades.exe`·`GameAssembly.dll`)의 고정 SHA-256
- 게임 폴더의 크랙·Steam 에뮬레이터 흔적
- Steam 설치 기록(설치 완료·업데이트 중 아님·소유 계정)과 그 계정의 Steam 로그인 기록

다음 두 가지는 Windows 전용이라 하지 않습니다.
- Steam 프로그램·`steamclient64.dll`의 Valve 디지털 서명 확인
- 게임의 `steam_api64.dll`로 실행 중인 Steam에 소유를 직접 묻는 확인

## 문제 해결
- **"만든 파일의 결과 해시가 manifest와 다릅니다"**: 아무 파일도 바꾸지 않고 멈춘 것입니다. 시스템 liblz4 버전에 따라 압축 결과가 달라질 수 있습니다(liblz4 1.10.0에서 확인). 메시지에 나온 liblz4 버전과 함께 Issues로 알려 주세요.
- **"지원하지 않는 버전"**: 게임이 업데이트되었거나 이전 버전 패치가 남아 있는 경우입니다. 무결성 검사를 한 뒤 다시 시도하거나 새 버전 패치를 기다려 주세요.
- **"설치가 여러 곳에 있습니다"**: `--game`으로 게임 폴더를 지정하세요.
