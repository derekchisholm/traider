"""A small VPC for the bot: two public subnets, no NAT gateway, nothing inbound.

The task gets a public IP so it can reach Schwab and AWS without a NAT gateway
(about 32 dollars a month saved). Its security group has no ingress rules at
all, and only allows HTTPS out.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass(frozen=True)
class Network:
    subnet_ids: list[pulumi.Output[str]]
    security_group_id: pulumi.Output[str]


def build(prefix: str, tags: dict[str, str]) -> Network:
    vpc = aws.ec2.Vpc(
        "vpc",
        cidr_block="10.42.0.0/24",
        enable_dns_support=True,
        enable_dns_hostnames=True,
        tags={**tags, "Name": prefix},
    )
    gateway = aws.ec2.InternetGateway("internet", vpc_id=vpc.id, tags={**tags, "Name": prefix})
    routes = aws.ec2.RouteTable(
        "public",
        vpc_id=vpc.id,
        routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=gateway.id)],
        tags={**tags, "Name": f"{prefix}-public"},
    )
    zones = aws.get_availability_zones_output(state="available")
    subnet_ids = []
    for index, cidr in enumerate(("10.42.0.0/25", "10.42.0.128/25")):
        subnet = aws.ec2.Subnet(
            f"public-{index}",
            vpc_id=vpc.id,
            cidr_block=cidr,
            availability_zone=zones.names[index],
            map_public_ip_on_launch=False,  # the ECS service asks for a public IP itself
            tags={**tags, "Name": f"{prefix}-public-{index}"},
        )
        aws.ec2.RouteTableAssociation(
            f"public-{index}", subnet_id=subnet.id, route_table_id=routes.id
        )
        subnet_ids.append(subnet.id)

    group = aws.ec2.SecurityGroup(
        "bot",
        vpc_id=vpc.id,
        description="traider bot: no inbound, HTTPS outbound",
        egress=[
            aws.ec2.SecurityGroupEgressArgs(
                protocol="tcp",
                from_port=443,
                to_port=443,
                cidr_blocks=["0.0.0.0/0"],
                description="HTTPS to Schwab and AWS APIs",
            )
        ],
        tags={**tags, "Name": f"{prefix}-bot"},
    )
    return Network(subnet_ids=subnet_ids, security_group_id=group.id)
