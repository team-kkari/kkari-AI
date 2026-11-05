"""/terraform — diagram → Terraform 변환. terraform CLI 가 있으면 fmt·validate 까지 확인 (TERRAFORM_BIN 으로 경로 지정 가능)."""
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from schemas import ArchitectureDraft, Constraints
from terraform_gen import generate

SAMPLES = Path(__file__).parent.parent / "data" / "samples"
TF = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")


def load(name):
    return ArchitectureDraft.model_validate(json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")))


def test_3tier_resources_follow_diagram_specs():
    a = load("architecture_3tier")
    out = generate(a.diagram, a.name)
    main = out["files"]["main.tf"]
    assert set(out["files"]) == {"versions.tf", "variables.tf", "main.tf", "outputs.tf", "README.md"}
    # 도식의 사양 그대로 (비용 계산과 같은 값)
    assert '"m7g.large"' in main and '"m7g.xlarge"' in main
    assert 'instance_class              = "db.r6g.xlarge"' in main and "multi_az                    = true" in main
    assert 'node_type                  = "cache.r7g.large"' in main and 'engine                     = "valkey"' in main
    assert "al2023-ami-kernel-default-arm64" in main          # Graviton → arm64 AMI
    # 보안 기본값
    assert "storage_encrypted           = true" in main and 'http_tokens = "required"' in main
    assert "block_public_acls       = true" in main and "manage_master_user_password = true" in main
    # 연결선 → 보안 그룹 규칙 (app → rds 3306)
    assert 'resource "aws_vpc_security_group_ingress_rule" "rds_from_app"' in main
    assert "from_port                    = 3306" in main
    # CloudFront 용 WAF 는 us-east-1
    assert re.search(r'scope\s+= "CLOUDFRONT"', main) and re.search(r'alias\s+= "us_east_1"', out["files"]["versions.tf"])


def test_frozen_resource_is_protected_not_recreated_silently():
    a = load("architecture_3tier")
    out = generate(a.diagram, a.name, Constraints(frozen_resources=["RDS prod-db MySQL 8.0 유지"]))
    main = out["files"]["main.tf"]
    assert "# [변경 금지] RDS prod-db MySQL 8.0 유지" in main
    assert "terraform import aws_db_instance.rds" in main and "prevent_destroy = true" in main


def test_serverless_without_vpc():
    a = load("architecture_serverless")
    out = generate(a.diagram, a.name)
    main = out["files"]["main.tf"]
    assert "aws_vpc" not in main and "aws_lambda_function" in main
    assert any("Lambda" in w for w in out["warnings"])


def test_korean_only_name_falls_back():
    a = load("architecture_serverless")
    assert generate(a.diagram, "서버리스 전환안")["project"] == "kkari"
    assert generate(a.diagram, "Graviton ASG + RDS")["project"] == "graviton-asg-rds"


def test_endpoint_json_and_zip(client):
    a = load("architecture_3tier").model_dump()
    r = client.post("/terraform", json=a)  # /architectures 의 architectures[i] 를 그대로 보내도 됨
    assert r.status_code == 200 and "main.tf" in r.json()["files"]
    r = client.post("/terraform", json={**a, "format": "zip"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "terraform.zip" in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "terraform/main.tf" in names and "terraform/README.md" in names


def test_endpoint_rejects_bad_diagram(client):
    r = client.post("/terraform", json={"name": "x", "diagram": {"zones": []}})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.skipif(not TF, reason="terraform CLI 없음 (TERRAFORM_BIN 으로 지정 가능)")
@pytest.mark.parametrize("sample", ["architecture_3tier", "architecture_serverless"])
def test_terraform_fmt_and_validate(sample, tmp_path):
    a = load(sample)
    c = Constraints(contains_personal_data=True, frozen_resources=["RDS prod-db"])
    for name, text in generate(a.diagram, a.name, c)["files"].items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", str(Path.home() / ".terraform.d" / "plugin-cache"))}
    Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run([TF, *args], cwd=tmp_path, env=env, capture_output=True, text=True)  # noqa: E731
    fmt = run("fmt", "-check", "-diff")
    assert fmt.returncode == 0, fmt.stdout
    init = run("init", "-backend=false", "-input=false", "-no-color")
    if init.returncode != 0:
        pytest.skip("terraform init 실패 (네트워크): " + init.stderr[-200:])
    val = run("validate", "-no-color")
    assert val.returncode == 0, val.stdout + val.stderr


def _serverless_aurora_diagram():
    from schemas import Diagram
    z = dict(spec="", engine="", multi_az=False, count=1, hours_per_month=730, storage_gb=0, data_gb_month=0,
             requests_million_month=0, write_requests_million_month=0, vcpu=0, memory_gb=0, duration_ms=0, lcu=0)
    node = lambda id, icon, place, tier="", zone="", role="", **p: {  # noqa: E731
        "id": id, "icon": icon, "label": id, "role": role, "place": place, "tier": tier, "zone": zone, "group": "",
        "pricing": {**z, **p}}
    return Diagram.model_validate({
        "zones": ["AZ1", "AZ2"],
        "tiers": [{"id": "app", "label": "App", "kind": "private"}, {"id": "db", "label": "DB", "kind": "private"}],
        "nodes": [node("apigw", "api_gateway", "region", kind="api_gateway"),
                  node("fn", "lambda", "subnet", "app", "each", kind="lambda", memory_gb=0.5),
                  node("vpce", "vpc_endpoint", "subnet", "app", "each", role="Secrets Manager·Logs 사설 접근",
                       kind="vpc_interface_endpoint"),
                  node("aurora", "aurora", "subnet", "db", "AZ1", role="Writer (min 0 ACU auto-pause)",
                       kind="aurora", spec="db.serverless", engine="Aurora PostgreSQL")],
        "groups": [],
        "edges": [{"source": "apigw", "target": "fn", "label": ""}, {"source": "fn", "target": "aurora", "label": ""},
                  {"source": "fn", "target": "vpce", "label": ""}]})


def test_serverless_aurora_and_vpc_lambda():
    """서버리스 + VPC Lambda: Aurora 용량 설정, Lambda VPC 연결·권한, 보안 그룹 규칙, 엔드포인트 추론, IGW 없음."""
    files = generate(_serverless_aurora_diagram(), "svl")["files"]
    main, var = files["main.tf"], files["variables.tf"]
    assert "serverlessv2_scaling_configuration" in main and re.search(r"min_capacity\s+= 0\n", main)
    assert "seconds_until_auto_pause" in main
    assert "vpc_config" in main and "AWSLambdaVPCAccessExecutionRole" in main
    assert 'resource "aws_vpc_security_group_ingress_rule" "aurora_from_fn"' in main
    assert 'resource "aws_vpc_security_group_ingress_rule" "vpce_from_fn"' in main
    assert "secretsmanager:GetSecretValue" in main and "master_user_secret[0].secret_arn" in main
    assert '["secretsmanager", "logs"]' in var        # "secrets" 안의 "ecr" 로 ECR 을 추측하지 않음
    assert "aws_internet_gateway" not in main          # public 서브넷이 없으면 IGW 도 없음
    assert "IMDSv2" not in files["README.md"]          # EC2 가 없으면 IMDSv2 안내도 없음


@pytest.mark.skipif(not TF, reason="terraform CLI 없음 (TERRAFORM_BIN 으로 지정 가능)")
def test_serverless_aurora_validates(tmp_path):
    for name, text in generate(_serverless_aurora_diagram(), "svl")["files"].items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", str(Path.home() / ".terraform.d" / "plugin-cache"))}
    Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run([TF, *a], cwd=tmp_path, env=env, capture_output=True, text=True)  # noqa: E731
    assert run("fmt", "-check", "-diff").returncode == 0
    if run("init", "-backend=false", "-input=false").returncode != 0:
        pytest.skip("terraform init 실패 (네트워크)")
    v = run("validate", "-no-color")
    assert v.returncode == 0, v.stdout + v.stderr
