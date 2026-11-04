# kkari-AI

AWS 인프라 최적화 서비스의 **AI 파트**: AWS 공식 Best Practice 문서를 수집해 RAG 로 검색하고, 사용자의 인프라 정보에 맞는 최적화 솔루션을 근거(출처 링크)와 함께 생성한다.

**설치·실행 → [INSTALL.md](INSTALL.md)**

## 구성

```
collect.py      AWS 문서 수집 (sources.py 의 목록)       → data/docs.jsonl
chunk.py        섹션 단위 청크 분할                        → data/chunks.jsonl
build_index.py  multilingual-e5-small 임베딩 인덱스 생성    → data/index/
retriever.py    하이브리드 검색 (벡터 + BM25, 카테고리 필터)
generator.py    Claude 로 솔루션/답변 생성 (LLM 교체 가능한 인터페이스)
intake.py       초기 상담 인터뷰 챗봇 (질문하며 요구사항 프로필 수집)
server.py       FastAPI: /intake, /recommend, /chat, /search
eval_search.py  검색 품질 스모크 테스트
download_model.py 임베딩 모델 이어받기 다운로더 → models/
```

## 데이터

| 구분 | 내용 |
|---|---|
| Well-Architected Framework | 비용 최적화 · 성능 효율성 · 안정성 · 보안 · 운영 우수성 · 지속 가능성 6개 기둥 + Serverless Lens (BP ID 포함) |
| 서비스별 Best Practices | EC2, S3, RDS, DynamoDB, Lambda, ECS, EKS, ElastiCache, IAM, VPC, CloudWatch 권장 알람, Trusted Advisor 비용 점검 |

문서 706개 → 청크 4,417개. 출처: [AWS Documentation](https://docs.aws.amazon.com/) (CC BY-SA 4.0).

- `chunks.jsonl` 필드: `source, category, service, bp_id, title, section, url, text, embed_text`
- 모든 파일 UTF-8

## 흐름

```
사용자 인프라 정보 + 원하는 방향 + 우선순위
        │
        ▼
  retriever: 우선순위 카테고리별 하이브리드 검색 → 근거 자료 12개
        │
        ▼
  generator: Claude 가 자료를 근거로 개선안 생성 (문제 · 방법 · 예상 효과 · 난이도 · 출처 번호)
        │
        ▼
  { solution, sources[] }  → 프론트에서 개선안 + "참고한 Best Practice" 링크 표시
```
