"""검색 품질 스모크 테스트 (API 키 불필요).

    uv run python eval_search.py

질문마다 상위 5개 안에 '기대 키워드'가 제목/본문에 있는 결과가 하나라도 있으면 통과.
"""
from retriever import Retriever

CASES = [
    ("EC2 CPU 사용률이 10% 밖에 안 되는데 비용을 줄이려면?", ["cost"], ["right siz", "rightsiz", "COST06"]),
    ("개발 서버를 밤에 꺼서 비용 절감", ["cost"], ["schedul", "stop", "turn off", "COST09", "COST07"]),
    ("스팟 인스턴스나 예약 인스턴스 같은 요금 모델 선택", ["cost"], ["spot", "reserved", "savings plan"]),
    ("S3 오래된 데이터 저렴한 스토리지로 옮기기", None, ["lifecycle", "glacier", "intelligent-tiering"]),
    ("NAT Gateway 데이터 전송 비용", None, ["nat gateway", "data transfer"]),
    ("RDS 장애 대비 고가용성 구성", None, ["multi-az"]),
    ("Lambda 콜드 스타트 줄이기", None, ["cold start", "provisioned concurrency", "init"]),
    ("IAM 최소 권한 원칙", ["security"], ["least privilege", "least-privilege"]),
    ("DynamoDB 파티션 키 설계 핫 파티션", None, ["partition key"]),
    ("오토스케일링으로 트래픽 변화 대응", None, ["auto scaling", "scal"]),
    ("CloudWatch 어떤 알람을 설정해야 하나", None, ["alarm"]),
    ("EBS gp2 에서 gp3 로 변경", None, ["gp3"]),
]


def main():
    r = Retriever()
    ok = 0
    for q, cats, keys in CASES:
        hits = r.search(q, k=5, categories=cats)
        hit = any(any(k.lower() in (h["title"] + h["text"]).lower() for k in keys) for h in hits)
        ok += hit
        print(f"{'✓' if hit else '✗'} {q}")
        for h in hits[:3]:
            print(f"     {h['score']:.4f} [{h['category']}] {h['bp_id'] or '':12} {h['title'][:70]}")
    print(f"\n{ok}/{len(CASES)} 통과")


if __name__ == "__main__":
    main()
