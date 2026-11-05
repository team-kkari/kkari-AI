"""/architectures 의 diagram → Terraform 코드 (AI 호출 없음, 결정적 변환).

    from terraform_gen import generate
    out = generate(diagram, name="Graviton ASG 안", constraints=constraints)
    out["files"]   # {"versions.tf": "...", "variables.tf": "...", "main.tf": "...", "outputs.tf": "...", "README.md": "..."}
    out["warnings"]  # 변환하지 못한 노드, 사람이 채워야 하는 값

원칙
- AWS provider 리소스만 사용 (외부 모듈 없음) → `terraform init` 만으로 validate 가능
- 사양(인스턴스 타입·대수·엔진·Multi-AZ·스토리지)은 diagram 의 pricing 값 그대로 → 비용 계산과 일치
- 보안 기본값: 저장 암호화, S3 퍼블릭 차단, IMDSv2, RDS 비밀번호는 Secrets Manager 관리(manage_master_user_password)
- 변경 금지 리소스(constraints.frozen_resources)는 prevent_destroy + import 안내 주석
- 자동 적용하지 않는다. 사람이 plan 을 확인하고 apply 한다
"""
from __future__ import annotations

import json
import re

from schemas import Constraints, Diagram, Node

ENGINE = {"MySQL": ("mysql", 3306), "PostgreSQL": ("postgres", 5432), "MariaDB": ("mariadb", 3306),
          "Aurora MySQL": ("aurora-mysql", 3306), "Aurora PostgreSQL": ("aurora-postgresql", 5432)}
CACHE = {"Valkey": ("valkey", 6379), "Redis": ("redis", 6379), "Memcached": ("memcached", 11211)}
SG_KINDS = {"alb", "nlb", "ec2", "rds", "aurora", "elasticache", "efs", "fargate", "vpc_interface_endpoint"}
DATA_KINDS = {"rds", "aurora", "elasticache", "elasticache_serverless", "efs"}
# CloudFront 관리형 정책 ID (AWS 고정값)
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER = "216adef6-5c7f-47e4-b989-5492eafa07d3"
FARGATE_CPU = [256, 512, 1024, 2048, 4096, 8192, 16384]
SKIP_ICONS = {"users", "internet", "igw", "shield", "auto_scaling", "ebs"}  # 다른 리소스에 포함되거나 비용·설정 불필요
TODO_ICONS = {"route53": "도메인(호스팅 영역)이 정해지면 aws_route53_record 로 ALB/CloudFront 별칭 레코드 추가",
              "acm": "도메인 확정 후 aws_acm_certificate 발급 → var.certificate_arn 에 입력",
              "eks": "EKS 는 클러스터·노드그룹·IAM 이 커서 terraform-aws-modules/eks 모듈 사용 권장",
              "cloudwatch": "알람·대시보드는 서비스 확정 후 aws_cloudwatch_metric_alarm 으로 추가",
              "backup": "aws_backup_plan + aws_backup_selection 으로 백업 정책 추가",
              "step_functions": "상태 머신 정의(ASL) 확정 후 aws_sfn_state_machine 추가",
              "eventbridge": "이벤트 패턴 확정 후 aws_cloudwatch_event_rule + target 추가",
              "iam": "역할·정책은 각 리소스 블록에 필요한 만큼 포함됨. 추가 권한은 최소 권한으로"}


def ident(s: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", s.lower()).strip("_") or "node"
    return s if not s[0].isdigit() else f"n_{s}"


def dash(s: str) -> str:
    return ident(s).replace("_", "-")


def q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def is_arm(instance_type: str) -> bool:
    return bool(re.search(r"\d+g[a-z]*$", instance_type.split(".")[-2] if "." in instance_type else instance_type))


def align(text: str) -> str:
    """terraform fmt 처럼 연속된 한 줄 속성의 '=' 를 맞춘다."""
    lines, out, group = text.split("\n"), [], []
    pat = re.compile(r"^(\s*)([A-Za-z_][\w-]*)\s*=\s(.*)$")

    def flush():
        if group:
            w = max(len(m.group(2)) for m in group)
            out.extend(f"{m.group(1)}{m.group(2).ljust(w)} = {m.group(3)}" for m in group)
            group.clear()
    for ln in lines:
        m = pat.match(ln)
        opens = m and m.group(3).rstrip().endswith(("[", "{", "(")) and not m.group(3).rstrip().endswith("{}")
        if m and group and len(m.group(1)) != len(group[0].group(1)):
            flush()
        if m and not opens:
            group.append(m)
            continue
        if m and opens:  # 여러 줄 값의 첫 줄은 정렬 대상이 아니다 (terraform fmt 와 동일)
            flush()
            out.append(f"{m.group(1)}{m.group(2)} = {m.group(3)}")
            continue
        flush()
        out.append(ln)
    flush()
    return "\n".join(out)


class _Gen:
    def __init__(self, d: Diagram, name: str, c: Constraints | None):
        self.d, self.c = d, c
        self.nodes = {n.id: n for n in d.nodes}
        self.blocks: list[str] = []
        self.outputs: list[str] = []
        self.warnings: list[str] = []
        self.vars: set[str] = {"region", "project"}
        proj = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20].strip("-")
        self.project = proj if proj and proj[0].isalpha() else "kkari"  # 한글만 있는 이름 등
        self.private_tiers = [t.id for t in d.tiers if t.kind == "private"]
        self.public_tiers = [t.id for t in d.tiers if t.kind == "public"]
        self.vpc = bool(d.zones and d.tiers)
        self.need_us_east_1 = False
        self.sg: dict[str, str] = {}
        self.tgs: dict[str, list[str]] = {}  # 타깃 노드 id → TG 리소스 주소 목록
        self.frozen = self._match_frozen()

    # ── 도우미 ──
    def out_edges(self, nid):
        return [e.target for e in self.d.edges if e.source == nid and e.target in self.nodes]

    def in_edges(self, nid):
        return [e.source for e in self.d.edges if e.target == nid and e.source in self.nodes]

    def tier_of(self, n: Node) -> str | None:
        if n.place == "subnet" and n.tier in {t.id for t in self.d.tiers}:
            return n.tier
        if not self.vpc:
            return None
        if n.pricing.kind in DATA_KINDS:  # region 에 그린 DB·캐시도 실제로는 VPC 안 → 마지막 private 층
            return (self.private_tiers or self.public_tiers)[-1]
        return (self.private_tiers or self.public_tiers)[0]

    def subnets(self, n: Node, tier: str) -> str:
        if n.zone in self.d.zones:
            return f"[aws_subnet.{ident(tier)}[{q(n.zone)}].id]"
        return f"[for s in aws_subnet.{ident(tier)} : s.id]"

    def tags(self, nid: str, label: str) -> str:
        return f'  tags = {{ Name = "${{local.name}}-{dash(nid)}", Role = {q(label)} }}'

    def lifecycle(self, nid: str) -> str:
        if nid not in self.frozen:
            return ""
        return "\n  lifecycle {\n    prevent_destroy = true\n  }"

    def _match_frozen(self) -> dict[str, str]:
        out = {}
        keys = {"rds": ["rds", "db", "mysql", "postgres", "mariadb"], "aurora": ["aurora", "rds"],
                "elasticache": ["elasticache", "redis", "valkey", "memcached", "cache"], "s3": ["s3", "bucket"],
                "ec2": ["ec2"], "efs": ["efs"], "dynamodb": ["dynamodb"]}
        for text in (self.c.frozen_resources or []) if self.c else []:
            low = text.lower()
            for n in self.d.nodes:
                if n.id.lower() in low or any(k in low for k in keys.get(n.pricing.kind, [])):
                    out.setdefault(n.id, text)
        return out

    # ── 네트워크 ──
    def network(self):
        z = self.d.zones
        azs = "\n".join(f"    {q(k)} = data.aws_availability_zones.available.names[{i}]" for i, k in enumerate(z))
        self.vars.add("vpc_cidr")
        b = [f'''data "aws_availability_zones" "available" {{
  state = "available"
}}

locals {{
  azs = {{
{azs}
  }}
}}

resource "aws_vpc" "main" {{
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {{ Name = local.name }}
}}''']
        if self.public_tiers:  # public 서브넷이 없으면 인터넷 게이트웨이도 만들지 않는다
            b.append('''resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags = { Name = local.name }
}''')
        for i, t in enumerate(self.d.tiers):
            b.append(f'''resource "aws_subnet" "{ident(t.id)}" {{
  for_each          = local.azs
  vpc_id            = aws_vpc.main.id
  availability_zone = each.value
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, {i * 10} + index(keys(local.azs), each.key))
  tags = {{ Name = "${{local.name}}-{dash(t.id)}-${{each.key}}", Tier = {q(t.label)} }}
}}''')
        if self.public_tiers:
            b.append('''resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}''')
        nats = [n for n in self.d.nodes if n.pricing.kind == "nat_gateway" or n.icon == "nat_gateway"]
        nat_zones = []
        if nats:
            n = nats[0]
            nat_zones = [n.zone] if n.zone in z else list(z)
            nat_tier = n.tier if n.tier in self.public_tiers else (self.public_tiers or [self.d.tiers[0].id])[0]
            nz = ", ".join(q(x) for x in nat_zones)
            mapping = "\n".join(f"    {q(k)} = {q(k if k in nat_zones else nat_zones[0])}" for k in z)
            b.append(f'''resource "aws_eip" "nat" {{
  for_each = toset([{nz}])
  domain   = "vpc"
  tags = {{ Name = "${{local.name}}-nat-${{each.key}}" }}
}}

resource "aws_nat_gateway" "main" {{
  for_each      = aws_eip.nat
  allocation_id = each.value.id
  subnet_id     = aws_subnet.{ident(nat_tier)}[each.key].id
  tags = {{ Name = "${{local.name}}-nat-${{each.key}}" }}
  depends_on = [aws_internet_gateway.main]
}}

locals {{
  nat_for_az = {{
{mapping}
  }}
}}''')
        if self.private_tiers:
            route = '''
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[local.nat_for_az[each.key]].id
  }''' if nats else ""
            b.append(f'''resource "aws_route_table" "private" {{
  for_each = local.azs
  vpc_id   = aws_vpc.main.id{route}
  tags = {{ Name = "${{local.name}}-private-${{each.key}}" }}
}}''')
        for t in self.d.tiers:
            rt = "aws_route_table.public.id" if t.kind == "public" else "aws_route_table.private[each.key].id"
            b.append(f'''resource "aws_route_table_association" "{ident(t.id)}" {{
  for_each       = aws_subnet.{ident(t.id)}
  subnet_id      = each.value.id
  route_table_id = {rt}
}}''')
        if len(z) < 2:
            self.warnings.append("AZ 가 1개라 ALB·RDS Multi-AZ 등 2개 AZ 가 필요한 리소스는 apply 시 실패할 수 있습니다.")
        self.blocks.append("# ───────── 네트워크 ─────────\n\n" + "\n\n".join(b))

    # ── 보안 그룹 ──
    def port_of(self, n: Node) -> str:
        k = n.pricing.kind
        if k in ("rds", "aurora"):
            return str(ENGINE.get(n.pricing.engine, ("", 3306))[1])
        if k in ("elasticache", "elasticache_serverless"):
            return str(CACHE.get(n.pricing.engine or "Valkey", ("", 6379))[1])
        if k == "efs":
            return "2049"
        if k == "vpc_interface_endpoint":
            return "443"
        if k in ("ec2", "fargate"):
            self.vars.add("app_port")
            return "var.app_port"
        return "80"

    def security_groups(self):
        if not self.vpc:
            return
        b = []
        for n in self.d.nodes:
            in_vpc_lambda = n.pricing.kind == "lambda" and n.place == "subnet"
            if (n.pricing.kind in SG_KINDS or in_vpc_lambda) and self.tier_of(n):
                name = ident(n.id)
                self.sg[n.id] = f"aws_security_group.{name}.id"
                b.append(f'''resource "aws_security_group" "{name}" {{
  name_prefix = "${{local.name}}-{dash(n.id)}-"
  description = {q(f"{n.label} ({n.role})" if n.role else n.label)}
  vpc_id      = aws_vpc.main.id
  tags = {{ Name = "${{local.name}}-{dash(n.id)}" }}
}}

resource "aws_vpc_security_group_egress_rule" "{name}_all" {{
  security_group_id = aws_security_group.{name}.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}}''')
        seen = set()
        for e in self.d.edges:
            src, dst = self.nodes.get(e.source), self.nodes.get(e.target)
            if not src or not dst or dst.id not in self.sg:
                continue
            port = self.port_of(dst)
            if src.id in self.sg:
                key = f"{ident(dst.id)}_from_{ident(src.id)}"
                if key in seen:
                    continue
                seen.add(key)
                b.append(f'''resource "aws_vpc_security_group_ingress_rule" "{key}" {{
  security_group_id            = aws_security_group.{ident(dst.id)}.id
  referenced_security_group_id = aws_security_group.{ident(src.id)}.id
  from_port                    = {port}
  to_port                      = {port}
  ip_protocol                  = "tcp"
}}''')
            elif dst.pricing.kind in ("alb", "nlb") and src.place in ("outside", "edge"):
                for p in (80, 443):
                    key = f"{ident(dst.id)}_public_{p}"
                    if key in seen:
                        continue
                    seen.add(key)
                    b.append(f'''resource "aws_vpc_security_group_ingress_rule" "{key}" {{
  security_group_id = aws_security_group.{ident(dst.id)}.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = {p}
  to_port           = {p}
  ip_protocol       = "tcp"
}}''')
        if b:
            self.blocks.append("# ───────── 보안 그룹 (diagram 의 연결선대로 필요한 포트만) ─────────\n\n" + "\n\n".join(b))

    # ── 리소스별 ──
    def alb(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 로드밸런서를 만들지 않았습니다.")
        internal = "true" if tier not in self.public_tiers else "false"
        lb_type = "network" if n.pricing.kind == "nlb" else "application"
        name = ident(n.id)
        targets = [self.nodes[t] for t in self.out_edges(n.id) if self.nodes[t].pricing.kind in ("ec2", "fargate")]
        b = [f'''resource "aws_lb" "{name}" {{
  name_prefix        = "{dash(n.id)[:6]}"
  load_balancer_type = "{lb_type}"
  internal           = {internal}
  security_groups    = [{self.sg[n.id]}]
  subnets            = [for s in aws_subnet.{ident(tier)} : s.id]
{self.tags(n.id, n.label)}
}}''']
        self.vars.add("app_port")
        proto = "TCP" if lb_type == "network" else "HTTP"
        for t in targets:
            tg = f"{name}_{ident(t.id)}"
            ttype = "ip" if t.pricing.kind == "fargate" else "instance"
            b.append(f'''resource "aws_lb_target_group" "{tg}" {{
  name_prefix = "{dash(t.id)[:6]}"
  port        = var.app_port
  protocol    = "{proto}"
  target_type = "{ttype}"
  vpc_id      = aws_vpc.main.id
  health_check {{
    path = "/"
  }}
}}''')
            self.tgs.setdefault(t.id, []).append(f"aws_lb_target_group.{tg}.arn")
        if targets:
            first = f"aws_lb_target_group.{name}_{ident(targets[0].id)}.arn"
            b.append(f'''resource "aws_lb_listener" "{name}_http" {{
  load_balancer_arn = aws_lb.{name}.arn
  port              = 80
  protocol          = "{proto}"
  default_action {{
    type             = "forward"
    target_group_arn = {first}
  }}
}}''')
            if lb_type == "application" and internal == "false":
                self.vars.add("certificate_arn")
                b.append(f'''# HTTPS: var.certificate_arn(ACM 인증서) 을 넣으면 생성된다
resource "aws_lb_listener" "{name}_https" {{
  count             = var.certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.{name}.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {{
    type             = "forward"
    target_group_arn = {first}
  }}
}}''')
            if len(targets) > 1:
                self.warnings.append(f"{n.label}: 대상이 {len(targets)}개라 첫 번째({targets[0].label})로만 기본 전달합니다. "
                                     "경로·호스트 기반 aws_lb_listener_rule 을 추가하세요.")
        else:
            self.warnings.append(f"{n.label}: 연결된 EC2/Fargate 가 없어 리스너를 만들지 않았습니다.")
        self.outputs.append(f'output "{name}_dns_name" {{\n  value = aws_lb.{name}.dns_name\n}}')
        self.blocks.append("\n\n".join(b))

    def ec2(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 EC2 를 만들지 않았습니다.")
        p, name = n.pricing, ident(n.id)
        itype = p.spec or "t4g.small"
        arch = "arm64" if is_arm(itype) else "x86_64"
        count = max(int(round(p.count)), 1)
        vol = max(int(p.storage_gb or 8), 8)
        tgs = ", ".join(self.tgs.get(n.id, []))
        b = [f'''resource "aws_launch_template" "{name}" {{
  name_prefix            = "${{local.name}}-{dash(n.id)}-"
  image_id               = data.aws_ssm_parameter.al2023_{arch}.value
  instance_type          = {q(itype)}
  vpc_security_group_ids = [{self.sg[n.id]}]
  block_device_mappings {{
    device_name = "/dev/xvda"
    ebs {{
      volume_size = {vol}
      volume_type = "gp3"
      encrypted   = true
    }}
  }}
  metadata_options {{
    http_tokens = "required"
  }}
  tag_specifications {{
    resource_type = "instance"
    tags = {{ Name = "${{local.name}}-{dash(n.id)}", Role = {q(n.role or n.label)} }}
  }}
}}''']
        self.amis.add(arch)
        b.append(f'''resource "aws_autoscaling_group" "{name}" {{
  name_prefix         = "${{local.name}}-{dash(n.id)}-"
  min_size            = {count}
  desired_capacity    = {count}
  max_size            = {count * 2}
  vpc_zone_identifier = {self.subnets(n, tier)}
  target_group_arns   = [{tgs}]
  health_check_type   = "{"ELB" if tgs else "EC2"}"
  launch_template {{
    id      = aws_launch_template.{name}.id
    version = "$Latest"
  }}
  tag {{
    key                 = "Name"
    value               = "${{local.name}}-{dash(n.id)}"
    propagate_at_launch = true
  }}{self.lifecycle(n.id)}
}}

resource "aws_autoscaling_policy" "{name}_cpu" {{
  name                   = "cpu-target-50"
  autoscaling_group_name = aws_autoscaling_group.{name}.name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {{
    predefined_metric_specification {{
      predefined_metric_type = "ASGAverageCPUUtilization"
    }}
    target_value = 50
  }}
}}''')
        if n.group not in {g.id for g in self.d.groups if g.kind == "auto_scaling"}:
            self.warnings.append(f"{n.label}: 도식에 Auto Scaling 그룹이 없지만 대수 관리를 위해 ASG(최소=최대/2={count})로 만들었습니다.")
        self.blocks.append("\n\n".join(b))

    def rds(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 RDS 를 만들지 않았습니다. VPC 를 추가하세요.")
        p, name = n.pricing, ident(n.id)
        engine = ENGINE.get(p.engine, ("mysql", 3306))[0]
        self.vars.add("db_username")
        sub = f'''resource "aws_db_subnet_group" "{name}" {{
  name_prefix = "${{local.name}}-{dash(n.id)}-"
  subnet_ids  = [for s in aws_subnet.{ident(tier)} : s.id]
}}'''
        user_attr = "master_username" if p.kind == "aurora" else "username"
        common = f'''  {user_attr} = var.db_username
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.{name}.name
  vpc_security_group_ids      = [{self.sg[n.id]}]
  storage_encrypted           = true
  backup_retention_period     = 7
  copy_tags_to_snapshot       = true
  deletion_protection         = true
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${{local.name}}-{dash(n.id)}-final"'''
        scaling = ""
        if p.kind == "aurora" and p.spec == "db.serverless":
            text = f"{n.label} {n.role}".lower()
            auto_pause = bool(re.search(r"(min\s*)?0\s*acu|auto-?pause|일시\s*정지", text))
            pause_line = "\n    seconds_until_auto_pause = 600" if auto_pause else ""
            scaling = f'''
  serverlessv2_scaling_configuration {{
    min_capacity = {0 if auto_pause else 0.5}
    max_capacity = 4{pause_line}
  }}'''
            if auto_pause:
                self.warnings.append(f"{n.label}: 0 ACU 자동 일시정지는 지원 버전(Aurora PostgreSQL 16.3+/15.7+, MySQL 3.08+)에서만 "
                                     "동작합니다. 필요하면 engine_version 을 지정하세요.")
        if p.kind == "aurora":
            body = f'''resource "aws_rds_cluster" "{name}" {{
  cluster_identifier          = "${{local.name}}-{dash(n.id)}"
  engine                      = {q(engine)}
{common}{scaling}{self.lifecycle(n.id)}
}}

resource "aws_rds_cluster_instance" "{name}" {{
  count              = {max(int(round(p.count)), 1)}
  identifier         = "${{local.name}}-{dash(n.id)}-${{count.index}}"
  cluster_identifier = aws_rds_cluster.{name}.id
  instance_class     = {q(p.spec or "db.r7g.large")}
  engine             = aws_rds_cluster.{name}.engine
}}'''
            self.outputs.append(f'output "{name}_endpoint" {{\n  value = aws_rds_cluster.{name}.endpoint\n}}')
        else:
            body = f'''resource "aws_db_instance" "{name}" {{
  identifier                  = "${{local.name}}-{dash(n.id)}"
  engine                      = {q(engine)}
  instance_class              = {q(p.spec or "db.t4g.medium")}
  multi_az                    = {str(bool(p.multi_az)).lower()}
  allocated_storage           = {max(int(p.storage_gb or 20), 20)}
  storage_type                = "gp3"
{common}{self.lifecycle(n.id)}
}}'''
            self.outputs.append(f'output "{name}_endpoint" {{\n  value = aws_db_instance.{name}.endpoint\n}}')
        if n.id in self.frozen:
            res = "aws_rds_cluster" if p.kind == "aurora" else "aws_db_instance"
            body = (f"# [변경 금지] {self.frozen[n.id]}\n# 새로 만들지 말고 기존 리소스를 가져와서 관리하세요:\n"
                    f"#   terraform import {res}.{name} <기존 식별자>\n# 가져온 뒤 plan 에서 엔진·버전 등 차이가 없게 값을 맞추세요.\n") + body
        self.blocks.append(sub + "\n\n" + body)

    def cache(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 ElastiCache 를 만들지 않았습니다.")
        p, name = n.pricing, ident(n.id)
        engine, port = CACHE.get(p.engine or "Valkey", ("valkey", 6379))
        count = max(int(round(p.count)), 1)
        sub = f'''resource "aws_elasticache_subnet_group" "{name}" {{
  name       = "${{local.name}}-{dash(n.id)}"
  subnet_ids = [for s in aws_subnet.{ident(tier)} : s.id]
}}'''
        if engine == "memcached":
            body = f'''resource "aws_elasticache_cluster" "{name}" {{
  cluster_id         = "${{local.name}}-{dash(n.id)}"
  engine             = "memcached"
  node_type          = {q(p.spec or "cache.t4g.small")}
  num_cache_nodes    = {count}
  port               = {port}
  subnet_group_name  = aws_elasticache_subnet_group.{name}.name
  security_group_ids = [{self.sg[n.id]}]{self.lifecycle(n.id)}
}}'''
        else:
            multi = str(count > 1).lower()
            body = f'''resource "aws_elasticache_replication_group" "{name}" {{
  replication_group_id       = "${{local.name}}-{dash(n.id)}"
  description                = {q(n.role or n.label)}
  engine                     = "{engine}"
  node_type                  = {q(p.spec or "cache.t4g.small")}
  num_cache_clusters         = {count}
  port                       = {port}
  automatic_failover_enabled = {multi}
  multi_az_enabled           = {multi}
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.{name}.name
  security_group_ids         = [{self.sg[n.id]}]{self.lifecycle(n.id)}
}}'''
            self.outputs.append(f'output "{name}_endpoint" {{\n  value = aws_elasticache_replication_group.{name}.primary_endpoint_address\n}}')
        self.blocks.append(sub + "\n\n" + body)

    def s3(self, n: Node):
        name = ident(n.id)
        self.blocks.append(f'''resource "aws_s3_bucket" "{name}" {{
  bucket_prefix = "${{local.name}}-{dash(n.id)[:12]}-"
{self.tags(n.id, n.role or n.label)}{self.lifecycle(n.id)}
}}

resource "aws_s3_bucket_public_access_block" "{name}" {{
  bucket                  = aws_s3_bucket.{name}.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "{name}" {{
  bucket = aws_s3_bucket.{name}.id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}

resource "aws_s3_bucket_versioning" "{name}" {{
  bucket = aws_s3_bucket.{name}.id
  versioning_configuration {{
    status = "Enabled"
  }}
}}''')
        if n.pricing.spec and n.pricing.spec not in ("standard", ""):
            self.warnings.append(f"{n.label}: 스토리지 클래스 {n.pricing.spec} 는 aws_s3_bucket_lifecycle_configuration 으로 전환 규칙을 추가하세요.")

    def efs(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 EFS 를 만들지 않았습니다.")
        name = ident(n.id)
        self.blocks.append(f'''resource "aws_efs_file_system" "{name}" {{
  encrypted = true
{self.tags(n.id, n.role or n.label)}{self.lifecycle(n.id)}
}}

resource "aws_efs_mount_target" "{name}" {{
  for_each        = aws_subnet.{ident(tier)}
  file_system_id  = aws_efs_file_system.{name}.id
  subnet_id       = each.value.id
  security_groups = [{self.sg[n.id]}]
}}''')

    def cloudfront(self, n: Node):
        name = ident(n.id)
        origins = [self.nodes[t] for t in self.out_edges(n.id) if self.nodes[t].pricing.kind in ("alb", "s3")
                   and (self.nodes[t].pricing.kind == "s3" or self.nodes[t].id in self.sg)]
        if not origins:
            return self.warnings.append(f"{n.label}: 연결된 ALB/S3 오리진이 없어 CloudFront 를 만들지 않았습니다.")
        o = origins[0]
        b = []
        if o.pricing.kind == "s3":
            origin = f'''  origin {{
    origin_id                = {q(o.id)}
    domain_name              = aws_s3_bucket.{ident(o.id)}.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.{name}.id
  }}'''
            methods, cache = '["GET", "HEAD"]', CACHING_OPTIMIZED
            b.append(f'''resource "aws_cloudfront_origin_access_control" "{name}" {{
  name                              = "${{local.name}}-{dash(n.id)}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}}

data "aws_iam_policy_document" "{name}_s3" {{
  statement {{
    actions   = ["s3:GetObject"]
    resources = ["${{aws_s3_bucket.{ident(o.id)}.arn}}/*"]
    principals {{
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }}
    condition {{
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.{name}.arn]
    }}
  }}
}}

resource "aws_s3_bucket_policy" "{name}" {{
  bucket = aws_s3_bucket.{ident(o.id)}.id
  policy = data.aws_iam_policy_document.{name}_s3.json
}}''')
            extra = ""
        else:
            origin = f'''  origin {{
    origin_id   = {q(o.id)}
    domain_name = aws_lb.{ident(o.id)}.dns_name
    custom_origin_config {{
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }}
  }}'''
            methods, cache = '["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]', CACHING_DISABLED
            extra = f"\n    origin_request_policy_id = {q(ALL_VIEWER)}"
            self.warnings.append(f"{n.label}: ALB 오리진은 http-only 로 연결했습니다. ALB 에 인증서를 붙이면 https-only 로 바꾸세요.")
        waf = next((self.nodes[s] for s in self.in_edges(n.id) if self.nodes[s].icon == "waf"), None)
        waf_line = f"\n  web_acl_id      = aws_wafv2_web_acl.{ident(waf.id)}.arn" if waf else ""
        b.insert(0, f'''resource "aws_cloudfront_distribution" "{name}" {{
  enabled         = true
  comment         = {q(n.role or n.label)}
  is_ipv6_enabled = true{waf_line}
{origin}
  default_cache_behavior {{
    target_origin_id       = {q(o.id)}
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = {methods}
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = {q(cache)}{extra}
  }}
  restrictions {{
    geo_restriction {{
      restriction_type = "none"
    }}
  }}
  viewer_certificate {{
    cloudfront_default_certificate = true
  }}
}}''')
        if len(origins) > 1:
            self.warnings.append(f"{n.label}: 오리진이 여러 개라 첫 번째({o.label})만 연결했습니다. ordered_cache_behavior 를 추가하세요.")
        self.outputs.append(f'output "{name}_domain_name" {{\n  value = aws_cloudfront_distribution.{name}.domain_name\n}}')
        self.blocks.append("\n\n".join(b))

    def waf(self, n: Node):
        name = ident(n.id)
        targets = [self.nodes[t] for t in self.out_edges(n.id)]
        to_cf = any(t.icon == "cloudfront" for t in targets)
        albs = [t for t in targets if t.pricing.kind == "alb" and t.id in self.sg]
        scope = "CLOUDFRONT" if to_cf else "REGIONAL"
        provider = "\n  provider = aws.us_east_1" if to_cf else ""
        self.need_us_east_1 |= to_cf
        b = [f'''resource "aws_wafv2_web_acl" "{name}" {{{provider}
  name  = "${{local.name}}-{dash(n.id)}"
  scope = "{scope}"
  default_action {{
    allow {{}}
  }}
  rule {{
    name     = "aws-common"
    priority = 1
    override_action {{
      none {{}}
    }}
    statement {{
      managed_rule_group_statement {{
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }}
    }}
    visibility_config {{
      cloudwatch_metrics_enabled = true
      metric_name                = "{name}-common"
      sampled_requests_enabled   = true
    }}
  }}
  visibility_config {{
    cloudwatch_metrics_enabled = true
    metric_name                = "{name}"
    sampled_requests_enabled   = true
  }}
}}''']
        for a in albs:
            b.append(f'''resource "aws_wafv2_web_acl_association" "{name}_{ident(a.id)}" {{
  resource_arn = aws_lb.{ident(a.id)}.arn
  web_acl_arn  = aws_wafv2_web_acl.{name}.arn
}}''')
        if not to_cf and not albs:
            self.warnings.append(f"{n.label}: 보호할 대상(CloudFront/ALB) 연결선이 없어 Web ACL 만 만들었습니다.")
        self.blocks.append("\n\n".join(b))

    def fargate(self, n: Node):
        tier = self.tier_of(n)
        if not tier:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 Fargate 서비스를 만들지 않았습니다.")
        p, name = n.pricing, ident(n.id)
        self.ecs_cluster()
        self.vars.update({"container_image", "app_port"})
        cpu = next((c for c in FARGATE_CPU if c >= (p.vcpu or 0.5) * 1024), 1024)
        mem = max(int((p.memory_gb or cpu / 512) * 1024), 512)
        arch = "ARM64" if p.spec == "arm" else "X86_64"
        lbs = "\n".join(f'''  load_balancer {{
    target_group_arn = {tg}
    container_name   = "app"
    container_port   = var.app_port
  }}''' for tg in self.tgs.get(n.id, []))
        self.blocks.append(f'''resource "aws_ecs_task_definition" "{name}" {{
  family                   = "${{local.name}}-{dash(n.id)}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "{cpu}"
  memory                   = "{mem}"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  runtime_platform {{
    operating_system_family = "LINUX"
    cpu_architecture        = "{arch}"
  }}
  container_definitions = jsonencode([{{
    name         = "app"
    image        = var.container_image
    essential    = true
    portMappings = [{{ containerPort = var.app_port }}]
  }}])
}}

resource "aws_ecs_service" "{name}" {{
  name            = "{dash(n.id)}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.{name}.arn
  desired_count   = {max(int(round(p.count)), 1)}
  launch_type     = "FARGATE"
  network_configuration {{
    subnets          = {self.subnets(n, tier)}
    security_groups  = [{self.sg[n.id]}]
    assign_public_ip = false
  }}
{lbs}
}}''')

    def ecs_cluster(self):
        if getattr(self, "_ecs", False):
            return
        self._ecs = True
        self.blocks.append('''resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name_prefix        = "${local.name}-ecs-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}''')

    def lambda_(self, n: Node):
        p, name = n.pricing, ident(n.id)
        self.vars.update({"lambda_package", "lambda_runtime"})
        if not getattr(self, "_lambda_role", False):
            self._lambda_role = True
            self.blocks.append('''data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name_prefix        = "${local.name}-lambda-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}''')
        mem = int((p.memory_gb or 0.5) * 1024)
        arch = "x86_64" if p.spec == "x86" else "arm64"
        vpc = ""
        if n.id in self.sg:  # private 서브넷에 둔 Lambda → VPC 연결 + ENI 권한
            vpc = f'''
  vpc_config {{
    subnet_ids         = {self.subnets(n, self.tier_of(n))}
    security_group_ids = [{self.sg[n.id]}]
  }}
  depends_on = [aws_iam_role_policy_attachment.lambda_vpc]'''
            if not getattr(self, "_lambda_vpc", False):
                self._lambda_vpc = True
                self.blocks.append('''resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}''')
        dbs = [self.nodes[t] for t in self.out_edges(n.id) if self.nodes[t].pricing.kind in ("rds", "aurora")
               and self.nodes[t].id in self.sg]
        if dbs:  # 연결된 DB 의 관리형 비밀번호(Secrets Manager)를 읽을 권한
            arns = ", ".join(f"aws_{'rds_cluster' if d.pricing.kind == 'aurora' else 'db_instance'}.{ident(d.id)}"
                             f".master_user_secret[0].secret_arn" for d in dbs)
            self.blocks.append(f'''data "aws_iam_policy_document" "{name}_db_secret" {{
  statement {{
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [{arns}]
  }}
}}

resource "aws_iam_role_policy" "{name}_db_secret" {{
  name_prefix = "db-secret-"
  role        = aws_iam_role.lambda.id
  policy      = data.aws_iam_policy_document.{name}_db_secret.json
}}''')
        self.blocks.append(f'''# TODO: var.lambda_package 에 배포 패키지(zip) 경로, handler 를 실제 코드에 맞게
resource "aws_lambda_function" "{name}" {{
  function_name = "${{local.name}}-{dash(n.id)}"
  role          = aws_iam_role.lambda.arn
  runtime       = var.lambda_runtime
  handler       = "app.handler"
  filename      = var.lambda_package
  architectures = ["{arch}"]
  memory_size   = {min(max(mem, 128), 10240)}
  timeout       = 30{vpc}
}}''')
        self.warnings.append(f"{n.label}: Lambda 코드 패키지(var.lambda_package)와 handler 를 채워야 apply 할 수 있습니다.")

    def api_gateway(self, n: Node):
        name = ident(n.id)
        b = [f'''resource "aws_apigatewayv2_api" "{name}" {{
  name          = "${{local.name}}-{dash(n.id)}"
  protocol_type = "HTTP"
}}

resource "aws_apigatewayv2_stage" "{name}" {{
  api_id      = aws_apigatewayv2_api.{name}.id
  name        = "$default"
  auto_deploy = true
}}''']
        fn = next((self.nodes[t] for t in self.out_edges(n.id) if self.nodes[t].pricing.kind == "lambda"), None)
        if fn:
            b.append(f'''resource "aws_apigatewayv2_integration" "{name}" {{
  api_id                 = aws_apigatewayv2_api.{name}.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.{ident(fn.id)}.invoke_arn
  payload_format_version = "2.0"
}}

resource "aws_apigatewayv2_route" "{name}" {{
  api_id    = aws_apigatewayv2_api.{name}.id
  route_key = "$default"
  target    = "integrations/${{aws_apigatewayv2_integration.{name}.id}}"
}}

resource "aws_lambda_permission" "{name}" {{
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.{ident(fn.id)}.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${{aws_apigatewayv2_api.{name}.execution_arn}}/*/*"
}}''')
        else:
            self.warnings.append(f"{n.label}: 연결된 Lambda 가 없어 통합(integration)을 만들지 않았습니다.")
        self.outputs.append(f'output "{name}_url" {{\n  value = aws_apigatewayv2_api.{name}.api_endpoint\n}}')
        self.blocks.append("\n\n".join(b))

    def simple(self, n: Node):
        name, k, icon = ident(n.id), n.pricing.kind, n.icon
        if k == "sqs" or icon == "sqs":
            self.blocks.append(f'''resource "aws_sqs_queue" "{name}_dlq" {{
  name                    = "${{local.name}}-{dash(n.id)}-dlq"
  sqs_managed_sse_enabled = true
}}

resource "aws_sqs_queue" "{name}" {{
  name                    = "${{local.name}}-{dash(n.id)}"
  sqs_managed_sse_enabled = true
  redrive_policy = jsonencode({{
    deadLetterTargetArn = aws_sqs_queue.{name}_dlq.arn
    maxReceiveCount     = 5
  }})
}}''')
        elif k == "dynamodb" or icon == "dynamodb":
            self.blocks.append(f'''# TODO: 실제 키 설계에 맞게 hash_key / range_key 수정
resource "aws_dynamodb_table" "{name}" {{
  name         = "${{local.name}}-{dash(n.id)}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {{
    name = "pk"
    type = "S"
  }}
  point_in_time_recovery {{
    enabled = true
  }}
  server_side_encryption {{
    enabled = true
  }}{self.lifecycle(n.id)}
}}''')
        elif icon == "sns":
            self.blocks.append(f'resource "aws_sns_topic" "{name}" {{\n  name = "${{local.name}}-{dash(n.id)}"\n}}')
        elif icon == "ecr":
            self.blocks.append(f'''resource "aws_ecr_repository" "{name}" {{
  name = "${{local.name}}-{dash(n.id)}"
  image_scanning_configuration {{
    scan_on_push = true
  }}
}}''')
        elif icon == "kms":
            self.blocks.append(f'''resource "aws_kms_key" "{name}" {{
  description         = {q(n.role or n.label)}
  enable_key_rotation = true
}}''')
        elif icon == "secrets_manager":
            self.blocks.append(f'resource "aws_secretsmanager_secret" "{name}" {{\n  name_prefix = "${{local.name}}-{dash(n.id)}-"\n}}')
        elif icon == "cognito":
            self.blocks.append(f'resource "aws_cognito_user_pool" "{name}" {{\n  name = "${{local.name}}-{dash(n.id)}"\n}}')
        elif icon == "vpc_endpoint":
            self.vpc_endpoint(n)
        else:
            return False
        return True

    def vpc_endpoint(self, n: Node):
        if not self.vpc:
            return self.warnings.append(f"{n.label}: VPC 가 없는 구성이라 엔드포인트를 만들지 않았습니다.")
        name = ident(n.id)
        text = f"{n.label} {n.role}".lower()
        if n.pricing.kind == "vpc_interface_endpoint":
            tier = self.tier_of(n)
            self.vars.add("interface_endpoint_services")
            guess = {r"secret": ["secretsmanager"], r"로그|\blogs?\b": ["logs"], r"\becr\b": ["ecr.api", "ecr.dkr"],
                     r"\bssm\b": ["ssm"], r"\bsts\b": ["sts"], r"\bkms\b": ["kms"], r"\bsqs\b": ["sqs"],
                     r"모니터링|\bmonitoring\b": ["monitoring"]}
            self.endpoint_services = list(dict.fromkeys(
                s for pat, v in guess.items() if re.search(pat, text, re.A) for s in v))
            self.blocks.append(f'''# 인터페이스 엔드포인트: var.interface_endpoint_services 에 서비스 이름을 넣으면 생성 (예: ["ecr.api", "ecr.dkr", "logs"])
resource "aws_vpc_endpoint" "{name}" {{
  for_each            = toset(var.interface_endpoint_services)
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${{var.region}}.${{each.value}}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [for s in aws_subnet.{ident(tier)} : s.id]
  security_group_ids  = [{self.sg[n.id]}]
}}''')
            if not self.endpoint_services:
                self.warnings.append(f"{n.label}: 인터페이스 엔드포인트 대상 서비스를 var.interface_endpoint_services 에 채우세요.")
            return
        svc = "dynamodb" if "dynamo" in text else "s3"
        pub = ["aws_route_table.public.id"] if self.public_tiers else []
        priv = "values(aws_route_table.private)[*].id" if self.private_tiers else ""
        rt = f"concat([{pub[0]}], {priv})" if pub and priv else (f"[{pub[0]}]" if pub else priv)
        self.blocks.append(f'''resource "aws_vpc_endpoint" "{name}" {{
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${{var.region}}.{svc}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = {rt}
}}''')

    # ── 조립 ──
    def build(self) -> dict:
        self.amis: set[str] = set()
        if self.vpc:
            self.network()
        elif any(n.place == "subnet" for n in self.d.nodes):
            self.warnings.append("도식에 zones/tiers 가 없어 서브넷 배치 노드를 VPC 없이 처리했습니다.")
        self.security_groups()
        handled = set()
        # ALB 를 먼저 만들어 대상 그룹을 EC2/Fargate 에 연결 → S3·Lambda 를 CloudFront·API GW 보다 먼저
        order = ["alb", "nlb", "ec2", "fargate", "rds", "aurora", "elasticache", "efs", "s3", "lambda"]
        for kind in order:
            for n in self.d.nodes:
                if n.pricing.kind != kind or n.id in handled:
                    continue
                handled.add(n.id)
                {"alb": self.alb, "nlb": self.alb, "ec2": self.ec2, "fargate": self.fargate, "rds": self.rds,
                 "aurora": self.rds, "elasticache": self.cache, "efs": self.efs, "s3": self.s3,
                 "lambda": self.lambda_}[kind](n)
        for n in self.d.nodes:
            if n.id in handled:
                continue
            handled.add(n.id)
            if n.icon == "cloudfront" or n.pricing.kind == "cloudfront":
                self.cloudfront(n)
            elif n.icon == "waf" or n.pricing.kind == "waf":
                self.waf(n)
            elif n.icon == "api_gateway" or n.pricing.kind == "api_gateway":
                self.api_gateway(n)
            elif n.pricing.kind == "nat_gateway" or n.icon in SKIP_ICONS or n.pricing.kind == "data_transfer_out":
                continue
            elif self.simple(n):
                continue
            elif n.icon in TODO_ICONS:
                self.warnings.append(f"{n.label}: {TODO_ICONS[n.icon]}")
            elif n.pricing.kind == "elasticache_serverless":
                self.warnings.append(f"{n.label}: ElastiCache Serverless 는 aws_elasticache_serverless_cache 로 추가하세요.")
            else:
                self.warnings.append(f"{n.label}({n.icon}): 자동 변환 대상이 아니라 직접 추가해야 합니다.")
        amis = "\n\n".join(f'''data "aws_ssm_parameter" "al2023_{a}" {{
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{a}"
}}''' for a in sorted(self.amis))
        if amis:
            self.blocks.insert(1 if self.vpc else 0, "# ───────── AMI (Amazon Linux 2023 최신) ─────────\n\n" + amis)
        return self.files()

    def files(self) -> dict:
        header = "# kkari-AI 가 추천 아키텍처 도식에서 생성한 Terraform 초안입니다. apply 전에 반드시 plan 을 검토하세요.\n"
        if self.frozen:
            header += "# 변경 금지 리소스: " + " / ".join(dict.fromkeys(self.frozen.values())) + "\n"
        main = header + '\nlocals {\n  name = var.project\n}\n\n' + "\n\n".join(self.blocks) + "\n"
        us = '''

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1" # CloudFront 용 WAF 는 us-east-1 에 만들어야 한다
}''' if self.need_us_east_1 else ""
        versions = f'''terraform {{
  required_version = ">= 1.5"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }}
  }}
}}

provider "aws" {{
  region = var.region
  default_tags {{
    tags = {{
      Project     = var.project
      ManagedBy   = "terraform"
      GeneratedBy = "kkari-AI"
    }}
  }}
}}{us}
'''
        var_defs = {
            "region": ('string', '"ap-northeast-2"', "배포 리전"),
            "project": ('string', q(self.project), "리소스 이름 접두사 (소문자·숫자·하이픈)"),
            "vpc_cidr": ('string', '"10.0.0.0/16"', "VPC CIDR"),
            "app_port": ('number', "80", "애플리케이션 포트 (ALB → 서버)"),
            "certificate_arn": ('string', '""', "ALB HTTPS 용 ACM 인증서 ARN (비우면 HTTPS 리스너 생략)"),
            "db_username": ('string', '"appadmin"', "DB 관리자 계정 (비밀번호는 Secrets Manager 가 자동 관리)"),
            "container_image": ('string', '"public.ecr.aws/nginx/nginx:stable"', "Fargate 컨테이너 이미지"),
            "lambda_package": ('string', '"lambda.zip"', "Lambda 배포 패키지 zip 경로"),
            "lambda_runtime": ('string', '"python3.12"', "Lambda 런타임"),
            "interface_endpoint_services": ('list(string)', json.dumps(getattr(self, "endpoint_services", [])), "인터페이스 VPC 엔드포인트 서비스 (예: [\"ecr.api\", \"logs\"])"),
        }
        variables = "\n\n".join(f'''variable "{k}" {{
  description = {q(var_defs[k][2])}
  type        = {var_defs[k][0]}
  default     = {var_defs[k][1]}
}}''' for k in var_defs if k in self.vars) + "\n"
        outputs = "\n\n".join(self.outputs) + "\n" if self.outputs else "# 출력 없음\n"
        readme = f'''# Terraform 초안 — {self.project}

kkari-AI `/architectures` 추천 결과의 도식을 변환한 **초안**입니다. 자동 적용하지 않습니다.

```bash
terraform init
terraform plan      # 반드시 검토
terraform apply
```

- 사양(인스턴스 타입·대수·엔진·Multi-AZ·스토리지)은 추천 결과의 비용 계산과 같은 값입니다
- 보안 기본값: 저장 암호화, S3 퍼블릭 차단{", IMDSv2" if self.amis else ""}, RDS 비밀번호는 Secrets Manager 자동 관리
- RDS/Aurora 는 deletion_protection 이 켜져 있습니다
- 사람이 채워야 할 항목:
''' + ("\n".join(f"  - {w}" for w in self.warnings) or "  - 없음") + "\n"
        return {k: align(v) if k.endswith(".tf") else v for k, v in {
            "versions.tf": versions, "variables.tf": variables, "main.tf": main,
            "outputs.tf": outputs, "README.md": readme}.items()}


def generate(diagram: Diagram, name: str = "kkari", constraints: Constraints | None = None) -> dict:
    g = _Gen(diagram, name, constraints)
    files = g.build()
    return {"project": g.project, "files": files, "warnings": g.warnings}
