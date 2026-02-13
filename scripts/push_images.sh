#!/usr/bin/env bash
#
# Retag local Docker images and push to DockerHub.
#
# Naming convention:
#   Base images:      DOCKERHUB_ORG/<short_name>:base
#   Milestone images: DOCKERHUB_ORG/<short_name>:<milestone_id>
#
# Usage:
#   ./scripts/push_images.sh                    # dry-run (default)
#   ./scripts/push_images.sh --push             # actually push
#   ./scripts/push_images.sh --push --repo navidrome   # push only navidrome
#   ./scripts/push_images.sh --push --repo navidrome --repo dubbo
#
set -euo pipefail

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
DOCKERHUB_ORG="${DOCKERHUB_ORG:-devevol}"

DRY_RUN=true
SELECTED_REPOS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --push)   DRY_RUN=false; shift ;;
        --repo)   SELECTED_REPOS+=("$2"); shift 2 ;;
        --org)    DOCKERHUB_ORG="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--push] [--repo <name>]... [--org <dockerhub_org>]"
            echo ""
            echo "  --push          Actually retag and push (default: dry-run)"
            echo "  --repo <name>   Only process this repo (can repeat). Options:"
            echo "                  navidrome, dubbo, ripgrep, go-zero, nushell, element-web, scikit-learn"
            echo "  --org <org>     DockerHub org (default: devevol)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ──────────────────────────────────────────────
# Image definitions
# ──────────────────────────────────────────────

# Each repo is defined as:
#   short_name | base_image | workspace_prefix | milestone_id_1 milestone_id_2 ...

declare -A REPO_BASE
declare -A REPO_PREFIX
declare -A REPO_MILESTONES

# 1. navidrome (~28 GB total)
REPO_BASE[navidrome]="navidrome_navidrome_v0.57.0_v0.58.0/base:latest"
REPO_PREFIX[navidrome]="navidrome_navidrome_v0.57.0_v0.58.0/baseline_004_v4"
REPO_MILESTONES[navidrome]="milestone_001 milestone_002 milestone_003_sub-01 milestone_003_sub-02 milestone_003_sub-03 milestone_003_sub-04 milestone_004 milestone_006 milestone_007"

# 2. dubbo (~35 GB total)
REPO_BASE[dubbo]="apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2_v2/base:latest"
REPO_PREFIX[dubbo]="apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2_v2"
REPO_MILESTONES[dubbo]="m001.1 m001.2 m003.1 m003.2 m003.3 m004 m006 m011 m016.1 m017 m018 m019 m025"

# 3. ripgrep (~42 GB total)
REPO_BASE[ripgrep]="burntsushi_ripgrep_14.1.1_15.0.0/base:latest"
REPO_PREFIX[ripgrep]="burntsushi_ripgrep_14.1.1_15.0.0/v1_001_v2"
REPO_MILESTONES[ripgrep]="milestone_seed_119407d_1_sub-01 milestone_seed_119407d_1_sub-02 milestone_seed_292bc54_1 milestone_seed_5f5da48_1_sub-01 milestone_seed_5f5da48_1_sub-02 milestone_seed_b610d1c_1 milestone_seed_2924d0c_1 milestone_seed_8c6595c_1 milestone_seed_a6e0be3_1_sub-01 milestone_seed_a6e0be3_1_sub-02 maintenance_style_1 maintenance_fixes_1_sub-01 maintenance_fixes_1_sub-02"

# 4. go-zero (~74 GB total)
REPO_BASE[go-zero]="zeromicro_go-zero_v1.6.0_v1.9.3/base:latest"
REPO_PREFIX[go-zero]="zeromicro_go-zero_v1.6.0_v1.9.3/baseline_001_rerun_stage4_008_fix"
REPO_MILESTONES[go-zero]="m001 m003 m004 m005 m007.1 m007.2 m008 m009 m010 m013 m014 m017 m018 m019 m020 m021 m022 m023 m024 m025 m026 m027 m028"

# 5. nushell (~158 GB total)
REPO_BASE[nushell]="nushell_nushell_0.106.0_0.108.0/base:latest"
REPO_PREFIX[nushell]="nushell_nushell_0.106.0_0.108.0/baseline_rerun_stage4_003_fixed_v2"
REPO_MILESTONES[nushell]="milestone_g01_48bca0a milestone_g02_da9615f milestone_g02_a647707 milestone_g04_1ddae02 milestone_g04_ca0e961 milestone_g05_0b8531e milestone_g05_be6e868 milestone_m02_parser milestone_m08_docs milestone_core_development.1 milestone_core_development.2 milestone_core_development.3 milestone_core_development.4"

# 6. element-web (~101 GB total)
REPO_BASE[element-web]="element-hq_element-web_v1.11.95_v1.11.97/base:latest"
REPO_PREFIX[element-web]="element-hq_element-web_v1.11.95_v1.11.97/v1_002"
REPO_MILESTONES[element-web]="milestone_seed_7ff1fd2_1 milestone_seed_e9a3625_1_sub-01 milestone_seed_e9a3625_1_sub-02 milestone_seed_e9a3625_1_sub-03 milestone_seed_be3778b_1 milestone_seed_56c7fc1_1 milestone_seed_fba5938_1 milestone_seed_aa99601_1 milestone_seed_f59af37_1 milestone_seed_8bb4d44_1 milestone_seed_e662c19_1 milestone_seed_3762d40_1 milestone_seed_599112e_1 maintenance_ui_ux feature_enhancements milestone_seed_3f47487_1 maintenance_bug_fixes milestone_seed_05df321_1"

# 7. scikit-learn (~70 GB total)
REPO_BASE[scikit-learn]="scikit-learn_scikit-learn_1.5.2_1.6.0/base:latest"
REPO_PREFIX[scikit-learn]="scikit-learn_scikit-learn_1.5.2_1.6.0/baseline_001_rerun_stage4_002"
REPO_MILESTONES[scikit-learn]="m01 m03 m04 m06 m11 m12.1 m12.2 m12.3 m12.4 m12.5 m13 m17"

# Ordered from smallest to largest
ALL_REPOS=(navidrome dubbo ripgrep scikit-learn go-zero element-web nushell)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

retag_and_push() {
    local src="$1"
    local dst="$2"

    # Check source exists
    if ! docker image inspect "$src" &>/dev/null; then
        echo -e "  ${RED}MISSING${NC}  $src"
        return 1
    fi

    if $DRY_RUN; then
        local size
        size=$(docker image inspect "$src" --format '{{.Size}}' 2>/dev/null | numfmt --to=iec-i --suffix=B 2>/dev/null || echo "?")
        echo -e "  ${CYAN}[dry-run]${NC}  $src  ->  ${GREEN}$dst${NC}  ($size)"
    else
        echo -e "  ${YELLOW}Tagging${NC}  $src  ->  $dst"
        docker tag "$src" "$dst"
        echo -e "  ${YELLOW}Pushing${NC}  $dst ..."
        docker push "$dst"
        echo -e "  ${GREEN}Done${NC}    $dst"
    fi
}

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

# Determine which repos to process
if [[ ${#SELECTED_REPOS[@]} -gt 0 ]]; then
    REPOS_TO_PROCESS=("${SELECTED_REPOS[@]}")
else
    REPOS_TO_PROCESS=("${ALL_REPOS[@]}")
fi

total_images=0
failed_images=0

for repo in "${REPOS_TO_PROCESS[@]}"; do
    if [[ -z "${REPO_BASE[$repo]+x}" ]]; then
        echo -e "${RED}Unknown repo: $repo${NC}"
        echo "Available: ${ALL_REPOS[*]}"
        exit 1
    fi

    echo ""
    echo -e "${GREEN}=== $repo ===${NC}"

    # Base image
    base_src="${REPO_BASE[$repo]}"
    base_dst="${DOCKERHUB_ORG}/${repo}:base"
    retag_and_push "$base_src" "$base_dst" || failed_images=$((failed_images + 1))
    total_images=$((total_images + 1))

    # Milestone images
    prefix="${REPO_PREFIX[$repo]}"
    for mid in ${REPO_MILESTONES[$repo]}; do
        src="${prefix}/${mid}:latest"
        dst="${DOCKERHUB_ORG}/${repo}:${mid}"
        retag_and_push "$src" "$dst" || failed_images=$((failed_images + 1))
        total_images=$((total_images + 1))
    done
done

echo ""
echo "──────────────────────────────────────────────"
if $DRY_RUN; then
    echo -e "${CYAN}DRY RUN complete.${NC} ${total_images} images would be processed ($failed_images missing)."
    echo "Run with --push to actually retag and push."
else
    echo -e "${GREEN}PUSH complete.${NC} ${total_images} images processed ($failed_images failed)."
fi
