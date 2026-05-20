#!/usr/bin/env bash
#
# eks-vpn-routes.sh — route EKS cluster API + VPC data-plane through the Opus2 VPN.
#
# WHY
# Two unreachable destinations when working with EKS dev clusters from home:
#
# (1) The EKS control plane. Cluster API endpoints are public but CIDR-
#     allowlisted to Opus2 office IPs. The corp OpenVPN is split-tunnel: it
#     routes ~150 hand-picked AWS IPs through the tunnel, but the specific
#     EKS API endpoint IPs your cluster uses may not be in that list. Without
#     a route, kubectl falls through to your default gateway and AWS sees
#     your home IP, which isn't allowlisted → connection times out.
#
# (2) Internal NLBs and pod IPs (the data plane). These live in private VPC
#     CIDRs (e.g. 10.35.0.0/16). The corp VPN doesn't push routes for those
#     CIDRs by default, so curling an internal NLB from your laptop falls
#     through to your home router and dies.
#
# This script:
#   1. Resolves your cluster's API endpoint IPs and adds /32 routes for each.
#   2. Resolves your cluster's VPC CIDR(s) and adds those as VPC routes too.
#   3. AWS now sees kubectl egressing from Opus2's NAT (allowlisted); curl
#      to internal NLBs / pods reaches the VPC through the VPN tunnel.
#
# Step 2 assumes the corp VPN actually has a path into the VPC (Site-to-Site
# VPN, TGW, or VPC peering). If your VPN doesn't peer with the VPC, the route
# is harmless — traffic just blackholes — and the script warns you on add.
# Skip step 2 entirely with SKIP_VPC_ROUTES=1 (kubectl-only mode).
#
# Reversibility
#   The routes live only in your laptop's kernel routing table. They
#   disappear on VPN disconnect, reboot, or `./eks-vpn-routes.sh remove`.
#   Nothing is changed in AWS or the VPN config file. Zero blast radius.
#
# Team use
#   Works for any team member on the same OpenVPN config. Per-cluster
#   differences go through env vars:
#       CLUSTER_NAME      default: DEV05-EKS-CLUSTER
#       AWS_REGION        default: eu-west-1
#       AWS_PROFILE       default: opus2-dev
#       VPN_IFACE         default: tun0
#       SKIP_VPC_ROUTES   set to non-empty to skip data-plane (VPC) routes
#
# Prerequisites
#   - VPN already connected (tun0 up)
#   - `aws sso login --profile <profile>` already run
#   - `dig` (debian: sudo apt-get install dnsutils)
#   - sudo (script auto-prompts for password if not cached)
#
# Usage
#   ./eks-vpn-routes.sh add      # add routes (default if no arg)
#   ./eks-vpn-routes.sh remove   # remove the routes
#   ./eks-vpn-routes.sh status   # show what's currently routed
#   ./eks-vpn-routes.sh --help
#
# DO NOT run via:
#   sh eks-vpn-routes.sh        # Dash doesn't support `set -o pipefail`
#   sudo ./eks-vpn-routes.sh    # sudo only needed for `ip route` calls inside

# Guard against invocation under sh/dash (which lacks pipefail). Must come
# BEFORE `set -o pipefail` or that line itself would be the error.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "ERROR: this script requires bash, not sh/dash." >&2
    echo "  Run with:  ./$(basename "$0") [add|remove|status]" >&2
    echo "  Or with:   bash $0 [add|remove|status]" >&2
    exit 1
fi

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-DEV05-EKS-CLUSTER}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
AWS_PROFILE="${AWS_PROFILE:-opus2-dev}"
VPN_IFACE="${VPN_IFACE:-tun0}"

# ANSI colors when stdout is a tty
if [ -t 1 ]; then
    G="$(tput setaf 2)"; Y="$(tput setaf 3)"; R="$(tput setaf 1)"; B="$(tput setaf 4)"; X="$(tput sgr0)"
else
    G=""; Y=""; R=""; B=""; X=""
fi

log()  { printf "%s\n" "$*"; }
ok()   { printf "${G}✓${X} %s\n" "$*"; }
warn() { printf "${Y}⚠${X} %s\n" "$*"; }
err()  { printf "${R}✗${X} %s\n" "$*" >&2; }
hdr()  { printf "\n${B}%s${X}\n" "$*"; }

usage() {
    sed -n '3,55p' "$0"
    exit 0
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { err "Required command not found: $1"; exit 2; }
}

# ---------- pre-flight ----------
preflight() {
    require_cmd aws
    require_cmd dig
    require_cmd ip

    if ! ip link show "$VPN_IFACE" >/dev/null 2>&1; then
        err "VPN interface '$VPN_IFACE' not found. Connect VPN first."
        exit 3
    fi

    if ! aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1; then
        err "AWS SSO session not active for profile '$AWS_PROFILE'."
        err "Run:  aws sso login --profile $AWS_PROFILE"
        exit 4
    fi
}

# Detect the VPN gateway by inspecting tun0's kernel-scope route.
# OpenVPN sets up `<subnet>/<prefix> dev tun0 proto kernel scope link src <client-ip>`;
# the gateway is the first usable address in that subnet.
detect_gateway() {
    local subnet first_three
    subnet=$(ip -4 route show dev "$VPN_IFACE" proto kernel 2>/dev/null \
             | awk '{print $1}' | head -1)
    if [ -z "$subnet" ]; then
        err "Could not detect VPN subnet on $VPN_IFACE."
        err "Set VPN_GATEWAY env var explicitly and rerun."
        exit 5
    fi
    first_three=$(echo "$subnet" | awk -F. '{print $1"."$2"."$3}')
    echo "${first_three}.1"
}

# Resolve cluster API endpoint hostname via aws, then dig.
resolve_endpoint_ips() {
    local endpoint hostname ips
    endpoint=$(aws eks describe-cluster \
        --name "$CLUSTER_NAME" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query 'cluster.endpoint' \
        --output text 2>/dev/null) || {
        err "aws eks describe-cluster failed. Check cluster name + permissions."
        exit 6
    }
    hostname=$(echo "$endpoint" | sed -E 's|^https?://||; s|:.*$||; s|/.*$||')
    if [ -z "$hostname" ]; then
        err "Could not parse hostname from endpoint: $endpoint"
        exit 6
    fi
    ips=$(dig +short "$hostname" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u)
    if [ -z "$ips" ]; then
        err "dig returned no IPs for $hostname"
        exit 7
    fi
    printf "%s\n" "$ips"
}

# Resolve the VPC ID for the cluster (cached on first call for the run).
resolve_vpc_id() {
    if [ -n "${_CACHED_VPC_ID:-}" ]; then
        echo "$_CACHED_VPC_ID"
        return
    fi
    local vpc_id
    vpc_id=$(aws eks describe-cluster \
        --name "$CLUSTER_NAME" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query 'cluster.resourcesVpcConfig.vpcId' \
        --output text 2>/dev/null) || {
        err "aws eks describe-cluster failed (looking up VPC ID)."
        exit 8
    }
    if [ -z "$vpc_id" ] || [ "$vpc_id" = "None" ]; then
        err "Could not determine VPC ID for cluster $CLUSTER_NAME"
        exit 8
    fi
    _CACHED_VPC_ID="$vpc_id"
    echo "$vpc_id"
}

# Resolve the associated CIDR block(s) for the cluster's VPC. A VPC can have
# a primary CIDR plus secondary CIDRs (AssociateVpcCidrBlock); we route all
# that are in `associated` state.
resolve_vpc_cidrs() {
    local vpc_id cidrs
    vpc_id=$(resolve_vpc_id)
    cidrs=$(aws ec2 describe-vpcs \
        --vpc-ids "$vpc_id" \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query 'Vpcs[0].CidrBlockAssociationSet[?CidrBlockState.State==`associated`].CidrBlock' \
        --output text 2>/dev/null) || {
        err "aws ec2 describe-vpcs failed for $vpc_id"
        exit 9
    }
    if [ -z "$cidrs" ]; then
        err "VPC $vpc_id has no associated CIDR blocks?"
        exit 9
    fi
    # aws --output text returns tab-separated; normalise to one per line.
    printf "%s\n" "$cidrs" | tr '\t' '\n'
}

# Probe the first internal NLB in the cluster's VPC to confirm the VPN
# actually has a data-plane path. Echoes "OK <dns> <ip>:<port>" on success,
# "BLACKHOLE <dns> <ip>" if route exists but traffic is dropped, or "NONE"
# if there's no internal NLB to probe.
probe_internal_nlb() {
    local vpc_id nlb_dns nlb_ip
    vpc_id=$(resolve_vpc_id)
    nlb_dns=$(aws elbv2 describe-load-balancers \
        --region "$AWS_REGION" \
        --profile "$AWS_PROFILE" \
        --query "LoadBalancers[?Scheme=='internal' && VpcId=='$vpc_id'] | [0].DNSName" \
        --output text 2>/dev/null)
    if [ -z "$nlb_dns" ] || [ "$nlb_dns" = "None" ]; then
        echo "NONE"
        return
    fi
    nlb_ip=$(dig +short "$nlb_dns" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    if [ -z "$nlb_ip" ]; then
        echo "NONE"
        return
    fi
    for port in 80 443; do
        if timeout 5 bash -c "</dev/tcp/$nlb_ip/$port" 2>/dev/null; then
            echo "OK $nlb_dns $nlb_ip:$port"
            return
        fi
    done
    echo "BLACKHOLE $nlb_dns $nlb_ip"
}

# ---------- subcommands ----------
cmd_add() {
    preflight
    local gateway ips cidrs probe
    gateway="${VPN_GATEWAY:-$(detect_gateway)}"
    hdr "VPN gateway: $gateway   interface: $VPN_IFACE"
    hdr "Resolving $CLUSTER_NAME API endpoint IPs..."
    ips=$(resolve_endpoint_ips)
    printf "%s\n" "$ips" | sed 's/^/  /'

    hdr "Adding control-plane /32 routes..."
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        if ip route show "$ip" 2>/dev/null | grep -q "dev $VPN_IFACE"; then
            ok "already routed: $ip"
        else
            sudo ip route add "$ip/32" via "$gateway" dev "$VPN_IFACE" \
                && ok "added: $ip via $gateway ($VPN_IFACE)" \
                || warn "failed to add $ip (already exists with different gateway?)"
        fi
    done <<< "$ips"

    if [ -z "${SKIP_VPC_ROUTES:-}" ]; then
        hdr "Resolving VPC CIDRs (data plane: internal NLBs, pods)..."
        cidrs=$(resolve_vpc_cidrs)
        printf "%s\n" "$cidrs" | sed 's/^/  /'

        hdr "Adding data-plane VPC CIDR routes..."
        while IFS= read -r cidr; do
            [ -z "$cidr" ] && continue
            if ip route show "$cidr" 2>/dev/null | grep -q "dev $VPN_IFACE"; then
                ok "already routed: $cidr"
            else
                sudo ip route add "$cidr" via "$gateway" dev "$VPN_IFACE" \
                    && ok "added: $cidr via $gateway ($VPN_IFACE)" \
                    || warn "failed to add $cidr (overlapping route?)"
            fi
        done <<< "$cidrs"
    else
        warn "SKIP_VPC_ROUTES set — skipping data-plane CIDR routes (kubectl only)"
    fi

    hdr "Verifying kubectl reachability..."
    if kubectl auth can-i get pods >/dev/null 2>&1; then
        ok "kubectl can reach the cluster"
    else
        warn "kubectl still failing. Check:"
        warn "  kubectl config current-context"
        warn "  aws eks update-kubeconfig --name $CLUSTER_NAME --region $AWS_REGION --profile $AWS_PROFILE"
    fi

    if [ -z "${SKIP_VPC_ROUTES:-}" ]; then
        hdr "Verifying data-plane reachability (probing an internal NLB)..."
        probe=$(probe_internal_nlb)
        case "$probe" in
            OK\ *)        ok "TCP reachable: ${probe#OK }" ;;
            BLACKHOLE\ *) warn "route exists but ${probe#BLACKHOLE } is unreachable."
                          warn "Your VPN likely doesn't peer with this VPC. Talk to whoever"
                          warn "runs the corp VPN — they need a Site-to-Site VPN, TGW"
                          warn "attachment, or VPC peering. Fall back to: kubectl port-forward." ;;
            NONE)         warn "No internal NLB found in VPC — reachability not verified." ;;
        esac
    fi
}

cmd_remove() {
    preflight
    local ips cidrs
    hdr "Resolving $CLUSTER_NAME API endpoint IPs..."
    ips=$(resolve_endpoint_ips)
    printf "%s\n" "$ips" | sed 's/^/  /'

    hdr "Removing control-plane /32 routes..."
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        if ip route show "$ip/32" 2>/dev/null | grep -q "dev $VPN_IFACE"; then
            sudo ip route del "$ip/32" && ok "removed: $ip"
        else
            ok "not present: $ip"
        fi
    done <<< "$ips"

    if [ -z "${SKIP_VPC_ROUTES:-}" ]; then
        hdr "Resolving VPC CIDRs..."
        cidrs=$(resolve_vpc_cidrs)
        printf "%s\n" "$cidrs" | sed 's/^/  /'

        hdr "Removing data-plane VPC CIDR routes..."
        while IFS= read -r cidr; do
            [ -z "$cidr" ] && continue
            if ip route show "$cidr" 2>/dev/null | grep -q "dev $VPN_IFACE"; then
                sudo ip route del "$cidr" && ok "removed: $cidr"
            else
                ok "not present: $cidr"
            fi
        done <<< "$cidrs"
    fi
}

cmd_status() {
    preflight
    local ips cidrs probe
    ips=$(resolve_endpoint_ips)
    hdr "$CLUSTER_NAME API endpoint IPs:"
    printf "%s\n" "$ips" | sed 's/^/  /'

    hdr "Current routes for control-plane IPs:"
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        ip route show "$ip" 2>/dev/null | sed 's/^/  /' || true
    done <<< "$ips"

    if [ -z "${SKIP_VPC_ROUTES:-}" ]; then
        cidrs=$(resolve_vpc_cidrs)
        hdr "VPC CIDRs (data plane):"
        printf "%s\n" "$cidrs" | sed 's/^/  /'

        hdr "Current routes for VPC CIDRs:"
        while IFS= read -r cidr; do
            [ -z "$cidr" ] && continue
            ip route show "$cidr" 2>/dev/null | sed 's/^/  /' || true
        done <<< "$cidrs"
    fi

    hdr "kubectl reachability:"
    if kubectl auth can-i get pods >/dev/null 2>&1; then
        ok "reachable"
    else
        warn "unreachable — run '$0 add' to fix"
    fi

    if [ -z "${SKIP_VPC_ROUTES:-}" ]; then
        hdr "Data-plane reachability (internal NLB probe):"
        probe=$(probe_internal_nlb)
        case "$probe" in
            OK\ *)        ok "TCP reachable: ${probe#OK }" ;;
            BLACKHOLE\ *) warn "route present but ${probe#BLACKHOLE } unreachable (VPN not peered with VPC?)" ;;
            NONE)         warn "no internal NLB to probe" ;;
        esac
    fi
}

# ---------- entrypoint ----------
case "${1:-add}" in
    add)    cmd_add ;;
    remove|rm|del) cmd_remove ;;
    status) cmd_status ;;
    -h|--help|help) usage ;;
    *)      err "Unknown subcommand: $1"; usage ;;
esac
