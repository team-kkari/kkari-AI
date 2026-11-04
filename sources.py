"""수집 대상 목록.

각 항목은 AWS 문서 가이드 하나 안에서 '시작 페이지'를 가리킨다.
수집기는 그 가이드의 toc-contents.json 에서 시작 페이지를 찾아 하위 트리 전체를 긁는다.
root 가 None 이면 가이드 전체를 긁는다.

category: 챗봇이 어떤 관점(비용/성능/보안 등)의 근거로 쓸지 필터링할 때 사용.
"""

BASE = "https://docs.aws.amazon.com"

# 1순위: Well-Architected Framework 6개 기둥 (BP ID 가 붙어 있음)
WELL_ARCHITECTED = [
    dict(name="wa-cost", guide="wellarchitected/latest/cost-optimization-pillar", root=None, category="cost"),
    dict(name="wa-performance", guide="wellarchitected/latest/performance-efficiency-pillar", root=None, category="performance"),
    dict(name="wa-reliability", guide="wellarchitected/latest/reliability-pillar", root=None, category="reliability"),
    dict(name="wa-security", guide="wellarchitected/latest/security-pillar", root=None, category="security"),
    dict(name="wa-operational", guide="wellarchitected/latest/operational-excellence-pillar", root=None, category="operations"),
    dict(name="wa-sustainability", guide="wellarchitected/latest/sustainability-pillar", root=None, category="sustainability"),
    dict(name="wa-serverless-lens", guide="wellarchitected/latest/serverless-applications-lens", root=None, category="serverless"),
]

# 2순위: 서비스별 Best Practices 페이지 (하위 트리 포함)
SERVICES = [
    dict(name="ec2", guide="AWSEC2/latest/UserGuide", root="ec2-best-practices.html", category="compute", service="EC2"),
    dict(name="s3-security", guide="AmazonS3/latest/userguide", root="security-best-practices.html", category="security", service="S3"),
    dict(name="s3-performance", guide="AmazonS3/latest/userguide", root="optimizing-performance.html", category="performance", service="S3"),
    dict(name="s3-storage-class", guide="AmazonS3/latest/userguide", root="storage-class-intro.html", category="cost", service="S3"),
    dict(name="rds", guide="AmazonRDS/latest/UserGuide", root="CHAP_BestPractices.html", category="database", service="RDS"),
    dict(name="dynamodb", guide="amazondynamodb/latest/developerguide", root="best-practices.html", category="database", service="DynamoDB"),
    dict(name="lambda", guide="lambda/latest/dg", root="best-practices.html", category="serverless", service="Lambda"),
    dict(name="ecs", guide="AmazonECS/latest/developerguide", root="ecs-best-practices.html", category="container", service="ECS"),
    dict(name="eks", guide="eks/latest/best-practices", root=None, category="container", service="EKS"),
    dict(name="elasticache", guide="AmazonElastiCache/latest/dg", root="BestPractices.html", category="database", service="ElastiCache"),
    dict(name="iam", guide="IAM/latest/UserGuide", root="best-practices.html", category="security", service="IAM"),
    dict(name="vpc-security", guide="vpc/latest/userguide", root="vpc-security-best-practices.html", category="network", service="VPC"),
    dict(name="cloudwatch-alarms", guide="AmazonCloudWatch/latest/monitoring", root="Best_Practice_Recommended_Alarms_AWS_Services.html", category="operations", service="CloudWatch"),
    dict(name="trusted-advisor-cost", guide="awssupport/latest/user", root="cost-optimization-checks.html", category="cost", service="TrustedAdvisor"),
]

SOURCES = WELL_ARCHITECTED + SERVICES
