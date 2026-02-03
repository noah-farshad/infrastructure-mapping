# Infrastructure Mapping Tool

**Version:** 1.4.0  
**Author:** Noah Farshad (noah@essential.coach)  
**License:** GPL v3

A standalone "Swiss Army knife" tool for managing VMware Aria Automation infrastructure configuration. Handles everything needed to go from zero to VM-deployment-ready.

---

## Features

| Component | Description |
|-----------|-------------|
| **Flavors** | VM sizing profiles (CPU/memory combinations) |
| **Images** | VM template mappings per region |
| **Storage** | Storage profile creation and tagging |
| **Tags** | Capability tags for cloud zones, network profiles, compute clusters |

All operations are **idempotent** - safe to run multiple times.

---

## Quick Start

```bash
# 1. Copy example config
cp configs/config_tx_example.yaml configs/config.yaml     # Single DC
cp configs/config_multi_dc_example.yaml configs/config.yaml  # Multi-DC

# 2. Edit and replace <REPLACE_...> values
vim configs/config.yaml

# 3. Discover what exists in your environment
python3 aria_mapping.py --config configs/config.yaml --list-regions
python3 aria_mapping.py --config configs/config.yaml --list-flavors
python3 aria_mapping.py --config configs/config.yaml --list-images
python3 aria_mapping.py --config configs/config.yaml --list-storage
python3 aria_mapping.py --config configs/config.yaml --list-tags

# 4. Preview changes (always do this first!)
python3 aria_mapping.py --config configs/config.yaml --all --dry-run

# 5. Execute
python3 aria_mapping.py --config configs/config.yaml --all --execute
```

---

## Command Reference

### List Commands (read-only)

```bash
--list-regions    # Show available cloud regions
--list-flavors    # Show existing flavor profiles
--list-images     # Show vCenter templates available for mapping
--list-storage    # Show storage profiles with details
--list-tags       # Show tags on cloud zones, network profiles, computes
```

### Process Commands (require --dry-run or --execute)

```bash
--flavors         # Process flavor profiles only
--images          # Process image profiles only
--storage         # Process storage profiles (create/update)
--tags            # Process capability tags
--all             # Process ALL components
```

### Execution Modes

```bash
--dry-run         # Preview changes without making them
--execute         # Apply changes
--verbose         # Show detailed API output for troubleshooting
```

---

## Workflow

### Phase 1: Discovery

```bash
# See what regions are available
python3 aria_mapping.py --config configs/config.yaml --list-regions

# See what templates exist for image mapping
python3 aria_mapping.py --config configs/config.yaml --list-images

# See current tag state
python3 aria_mapping.py --config configs/config.yaml --list-tags
```

### Phase 2: Preview Changes

```bash
# Preview each component separately
python3 aria_mapping.py --config configs/config.yaml --flavors --dry-run
python3 aria_mapping.py --config configs/config.yaml --images --dry-run
python3 aria_mapping.py --config configs/config.yaml --storage --dry-run
python3 aria_mapping.py --config configs/config.yaml --tags --dry-run

# Or preview everything at once
python3 aria_mapping.py --config configs/config.yaml --all --dry-run
```

### Phase 3: Execute

```bash
# Execute one component at a time (recommended for troubleshooting)
python3 aria_mapping.py --config configs/config.yaml --flavors --execute
python3 aria_mapping.py --config configs/config.yaml --images --execute
python3 aria_mapping.py --config configs/config.yaml --storage --execute
python3 aria_mapping.py --config configs/config.yaml --tags --execute

# Or execute everything at once
python3 aria_mapping.py --config configs/config.yaml --all --execute
```

---

## Configuration Examples

### Single Datacenter (config_tx_example.yaml)

```yaml
regions:
  - name: "TX-DC01"

flavor_profile_name: "my-flavor-profile"
flavors:
  - name: "4cpu-16gb"
    cpuCount: 4
    memoryMB: 16384
```

### Multi-Datacenter (config_multi_dc_example.yaml)

```yaml
regions:
  - name: "TX-DC01"
  - name: "VA-DC01"

# Same flavors deployed to BOTH regions
flavor_profile_name: "my-flavor-profile"
flavors:
  - name: "4cpu-16gb"
    cpuCount: 4
    memoryMB: 16384
```

---

## Tag Configuration

Tags enable constraint-based VM placement in blueprints.

### Cloud Zones

```yaml
tags:
  cloud_zones:
    - name: "TX-W01-vcsa.example.com / TX-DC01"
      tags:
        - key: "location"
          value: "TX-SDDC"
```

### Storage Profiles with Compute Binding

```yaml
tags:
  storage_profiles:
    - name: "TX-SDDC-Oracle"
      compute: "TX-Oracle-Cluster"  # Bind to specific cluster
      tags:
        - key: "storage"
          value: "oracle"
```

### Creating New Storage Profiles

```yaml
tags:
  storage_profiles:
    - name: "VA-SDDC-Oracle"
      region: "VA-DC01"
      create: true                  # CREATE new profile
      description: "VA Oracle storage"
      provisioning_type: "thin"
      compute: "VA-Oracle-Cluster"
      default: false
      tags:
        - key: "storage"
          value: "oracle"
```

---

## API Reference

| Resource | Endpoint | Method |
|----------|----------|--------|
| Flavor Profiles | `/iaas/api/flavor-profiles` | POST (one per region) |
| Image Profiles | `/iaas/api/image-profiles` | POST (one per region) |
| Storage Profiles | `/iaas/api/storage-profiles` | POST/PUT |
| Cloud Zones | `/iaas/api/zones` | PATCH |
| Network Profiles | `/iaas/api/network-profiles` | PATCH |
| Compute Clusters | `/iaas/api/fabric-computes` | PATCH |

---

## Requirements

- Python 3.8+
- `requests` library
- `pyyaml` library

```bash
pip install requests pyyaml
```

---

## Troubleshooting

### "regionId is required"

Run `--list-regions` and verify your config uses the exact region names shown.

### "name is required" for Cloud Zones

Run `--list-tags` and copy the exact Cloud Zone name (including vCenter hostname).

### Template not found for Image mapping

Run `--list-images` and verify the template name exists in that region's vCenter.

### Verbose Mode

```bash
python3 aria_mapping.py --config config.yaml --storage --execute --verbose
```

---

## License

GPL v3 License - Copyright (C) 2026 Noah Farshad
