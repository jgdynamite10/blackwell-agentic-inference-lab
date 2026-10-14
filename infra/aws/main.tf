# D-0041 record of the closed session aws-diag-20261014a. No inbound
# ports. Serving stayed on loopback. OS shutdown terminated the instance,
# and the root volume was deleted with it. The precondition below refuses
# another plan or apply until a later decision removes it.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = ["us-east-1a"]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_security_group" "host" {
  name        = "bwlab-${var.run_tag}"
  description = "No inbound. Outbound only, for package, image, and model download."
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "Outbound package and model download"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

resource "aws_iam_role" "host" {
  name = "bwlab-${var.run_tag}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "secret" {
  name = "hf-token-read"
  role = aws_iam_role.host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.hf.arn
    }]
  })
}

resource "aws_iam_instance_profile" "host" {
  name = "bwlab-${var.run_tag}"
  role = aws_iam_role.host.name
}

resource "aws_secretsmanager_secret" "hf" {
  name                    = "bwlab/${var.run_tag}/hf-token"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_instance" "gpu" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  associate_public_ip_address = true
  user_data                   = file("${path.module}/user-data.sh")

  instance_initiated_shutdown_behavior = "terminate"

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_size           = 400
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.tags, { Name = "bwlab-${var.run_tag}" })

  lifecycle {
    ignore_changes = [ami, user_data]
    precondition {
      condition     = var.run_tag != "aws-diag-20261014a"
      error_message = "D-0041 is closed. This module records aws-diag-20261014a and refuses plan and apply until a new owner decision removes this precondition."
    }
  }
}

locals {
  tags = {
    project = "blackwell-lab"
    run     = var.run_tag
    ttl     = "10h-from-first-boot"
    phase   = "d-0041"
  }
}
