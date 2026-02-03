#!/usr/bin/env python3
"""
===============================================================================
VMware Aria Automation - Infrastructure Mapping Tool
===============================================================================

A standalone, portable tool for managing VMware Aria Automation infrastructure:
  - Flavor profiles (VM sizing)
  - Image profiles (VM templates)
  - Storage profiles (create and tag)
  - Capability tags (cloud zones, network profiles, compute clusters)

License: MIT
Author: Noah Farshad (noah@essential.coach)

===============================================================================
USAGE - Selective Processing
===============================================================================

LIST COMMANDS (read-only, no changes):
  --list-regions    Show available cloud regions
  --list-flavors    Show existing flavor profiles
  --list-images     Show vCenter templates
  --list-storage    Show storage profiles with details
  --list-tags       Show tags on cloud zones, network profiles, computes

PROCESS COMMANDS (require --dry-run or --execute):
  --flavors         Process flavor profiles only
  --images          Process image profiles only
  --storage         Process storage profiles only (create/update)
  --tags            Process capability tags only (cloud zones, network, compute)
  --all             Process ALL components

EXAMPLES:
  # Preview changes first (always recommended)
  python3 aria_mapping.py --config config.yaml --flavors --dry-run
  python3 aria_mapping.py --config config.yaml --storage --dry-run
  python3 aria_mapping.py --config config.yaml --tags --dry-run

  # Execute specific component
  python3 aria_mapping.py --config config.yaml --flavors --execute
  python3 aria_mapping.py --config config.yaml --storage --execute
  python3 aria_mapping.py --config config.yaml --tags --execute

  # Execute everything at once
  python3 aria_mapping.py --config config.yaml --all --execute

  # Verbose mode for troubleshooting
  python3 aria_mapping.py --config config.yaml --tags --execute --verbose

IDEMPOTENCY:
  All commands are idempotent - running them multiple times is safe.
  Existing resources with matching configuration are skipped.

===============================================================================
API NOTES
===============================================================================

- Flavor profiles: /iaas/api/flavor-profiles (one per region)
- Image profiles: /iaas/api/image-profiles (one per region)
- Storage profiles: /iaas/api/storage-profiles (POST to create, PUT to update)
- Cloud zones: /iaas/api/zones (PATCH to update tags)
- Network profiles: /iaas/api/network-profiles (PATCH to update tags)
- Compute clusters: /iaas/api/fabric-computes (PATCH to update tags)

===============================================================================
"""

import argparse
import json
import sys
from pathlib import Path

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__version__ = "1.4.0"
__author__ = "Noah Farshad"

API_VERSION = "2021-07-15"


def load_config(config_path: str) -> dict:
    """Load and validate YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    
    with open(path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Validate required sections
    required = ['aria', 'regions']
    missing = [r for r in required if r not in config]
    if missing:
        print(f"Error: Missing required config sections: {missing}")
        sys.exit(1)
    
    return config


class AriaClient:
    """
    VMware Aria Automation API Client
    
    Handles authentication and API calls for flavor/image mappings.
    Supports both vRA 8.x (username/password) authentication.
    """
    
    def __init__(self, host: str, username: str, password: str, 
                 domain: str = "System Domain", verify_ssl: bool = False):
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.domain = domain
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.token = None
    
    def authenticate(self) -> bool:
        """
        Authenticate to Aria Automation.
        
        Uses the two-step authentication:
        1. Get refresh token from /csp/gateway/am/api/login
        2. Exchange for bearer token via /iaas/api/login
        """
        try:
            # Step 1: Get refresh token
            url = f"https://{self.host}/csp/gateway/am/api/login?access_token"
            payload = {
                "username": self.username,
                "password": self.password,
                "domain": self.domain
            }
            
            resp = self.session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            refresh_token = data.get('refresh_token')
            if not refresh_token:
                # Some versions return access_token directly
                self.token = data.get('access_token')
                if not self.token:
                    print("Error: No token received from authentication")
                    return False
            else:
                # Step 2: Exchange refresh token for bearer token
                token_url = f"https://{self.host}/iaas/api/login"
                token_resp = self.session.post(
                    token_url, 
                    json={"refreshToken": refresh_token},
                    timeout=30
                )
                token_resp.raise_for_status()
                self.token = token_resp.json().get('token')
            
            if not self.token:
                print("Error: Failed to obtain bearer token")
                return False
            
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            })
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"Authentication failed: {e}")
            return False
    
    def _api_url(self, path: str) -> str:
        """Build API URL with version parameter."""
        separator = '&' if '?' in path else '?'
        return f"https://{self.host}{path}{separator}apiVersion={API_VERSION}"
    
    def _get_paginated(self, path: str) -> list:
        """Fetch all pages of a paginated API endpoint."""
        results = []
        url = self._api_url(path)
        
        while url:
            resp = self.session.get(url, timeout=60)
            if resp.status_code != 200:
                break
            
            data = resp.json()
            results.extend(data.get('content', []))
            
            # Check for next page
            links = data.get('_links', {})
            next_link = links.get('next', {}).get('href')
            if next_link:
                url = f"https://{self.host}{next_link}"
            else:
                url = None
        
        return results
    
    # =========================================================================
    # REGIONS
    # =========================================================================
    
    def get_regions(self) -> list:
        """Get all cloud account regions."""
        return self._get_paginated("/iaas/api/regions")
    
    # =========================================================================
    # FLAVORS
    # =========================================================================
    
    def get_flavor_profiles(self) -> list:
        """Get all existing flavor profiles."""
        return self._get_paginated("/iaas/api/flavor-profiles")
    
    def create_flavor_profile(self, profile_name: str, description: str, region_id: str,
                              flavors: list, verbose: bool = False) -> tuple:
        """
        Create a flavor profile for a single region with MULTIPLE flavors.
        
        CRITICAL DISCOVERY: The Aria API expects ALL flavors in a SINGLE call
        per region. Each call creates/replaces the flavor-profile for that region.
        
        Official VMware example shows:
        {
            "name": "vcenter-flavor-profile",
            "regionId": "uuid-here",
            "flavorMapping": {
                "small": { "cpuCount": 1, "memoryInMB": 1024 },
                "medium": { "cpuCount": 2, "memoryInMB": 2048 },
                "large": { "cpuCount": 4, "memoryInMB": 4096 }
            }
        }
        
        Args:
            profile_name: Name for the flavor profile (e.g., "vSphere-flavor-profile")
            description: Description of the profile
            region_id: Region UUID
            flavors: List of dicts with 'name', 'cpuCount', 'memoryMB' keys
            verbose: Show debug output
        """
        url = self._api_url("/iaas/api/flavor-profiles")
        
        # Build flavorMapping with ALL flavors
        flavor_mapping = {}
        for f in flavors:
            flavor_mapping[f['name']] = {
                "cpuCount": f['cpuCount'],
                "memoryInMB": f['memoryMB']
            }
        
        payload = {
            "name": profile_name,
            "description": description,
            "regionId": region_id,
            "flavorMapping": flavor_mapping
        }
        
        if verbose:
            print(f"\n    [DEBUG] POST {url}")
            print(f"    [DEBUG] Payload contains {len(flavors)} flavors")
            print(f"    [DEBUG] Flavor names: {list(flavor_mapping.keys())}")
        
        try:
            resp = self.session.post(url, json=payload, timeout=60)
            
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
                try:
                    resp_data = resp.json()
                    print(f"    [DEBUG] Response ID: {resp_data.get('id', 'N/A')}")
                    print(f"    [DEBUG] Response name: {resp_data.get('name', 'N/A')}")
                except:
                    print(f"    [DEBUG] Response: {resp.text[:300]}")
            
            if resp.status_code in [200, 201]:
                result = resp.json()
                return True, result
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)
    
    def delete_flavor_profile(self, profile_id: str) -> tuple:
        """Delete a flavor profile by ID."""
        url = self._api_url(f"/iaas/api/flavor-profiles/{profile_id}")
        try:
            resp = self.session.delete(url, timeout=30)
            if resp.status_code in [200, 204]:
                return True, "Deleted"
            else:
                error = resp.json().get('message', resp.text[:200])
                return False, error
        except Exception as e:
            return False, str(e)
    
    # =========================================================================
    # IMAGES
    # =========================================================================
    
    def get_image_profiles(self) -> list:
        """Get all existing image profiles."""
        return self._get_paginated("/iaas/api/image-profiles")
    
    def get_fabric_images(self) -> list:
        """Get all fabric images (vCenter templates)."""
        return self._get_paginated("/iaas/api/fabric-images")
    
    def get_fabric_images_lookup(self) -> dict:
        """
        Get fabric images and build a lookup by name.
        
        Returns: dict mapping (region_id, template_name) -> fabric_image_id
        """
        images = self.get_fabric_images()
        lookup = {}
        
        for img in images:
            name = img.get('name', '')
            # Get region from _links
            links = img.get('_links', {})
            region_href = links.get('region', {}).get('href', '')
            region_id = ''
            if '/regions/' in region_href:
                region_id = region_href.split('/regions/')[-1]
            
            img_id = img.get('id', '')
            external_region = img.get('externalRegionId', '')
            
            # Store by multiple keys for flexibility
            if name and img_id:
                lookup[(region_id, name)] = img_id
                lookup[(external_region, name)] = img_id
                # Also store just by name for simpler lookup
                if name not in lookup:
                    lookup[name] = {'id': img_id, 'region_id': region_id}
        
        return lookup

    def create_image_profile(self, profile_name: str, description: str, region_id: str,
                             images: list, verbose: bool = False) -> tuple:
        """
        Create an image profile for a single region with MULTIPLE images.
        
        CRITICAL: The API expects 'id' (fabric image ID), not 'imageName'.
        You must look up the fabric image ID from /iaas/api/fabric-images first.
        
        Args:
            profile_name: Name for the image profile
            description: Description of the profile
            region_id: Region UUID
            images: List of dicts with 'name' and 'id' keys (id = fabric image ID)
            verbose: Show debug output
        """
        url = self._api_url("/iaas/api/image-profiles")
        
        # Build imageMapping with ALL images using 'id'
        image_mapping = {}
        for img in images:
            image_mapping[img['name']] = {
                "id": img['id']
            }
        
        payload = {
            "name": profile_name,
            "description": description,
            "regionId": region_id,
            "imageMapping": image_mapping
        }
        
        if verbose:
            print(f"\n    [DEBUG] POST {url}")
            print(f"    [DEBUG] Payload contains {len(images)} images")
            print(f"    [DEBUG] Image mappings:")
            for img in images:
                print(f"             {img['name']} -> id: {img['id'][:20]}...")
        
        try:
            resp = self.session.post(url, json=payload, timeout=60)
            
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
                try:
                    resp_data = resp.json()
                    print(f"    [DEBUG] Response ID: {resp_data.get('id', 'N/A')}")
                    print(f"    [DEBUG] Response name: {resp_data.get('name', 'N/A')}")
                except:
                    print(f"    [DEBUG] Response: {resp.text[:300]}")
            
            if resp.status_code in [200, 201]:
                result = resp.json()
                return True, result
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)
    
    def delete_image_profile(self, profile_id: str) -> tuple:
        """Delete an image profile by ID."""
        url = self._api_url(f"/iaas/api/image-profiles/{profile_id}")
        try:
            resp = self.session.delete(url, timeout=30)
            if resp.status_code in [200, 204]:
                return True, "Deleted"
            else:
                error = resp.json().get('message', resp.text[:200])
                return False, error
        except Exception as e:
            return False, str(e)

    # =========================================================================
    # TAG MANAGEMENT METHODS
    # =========================================================================

    def get_cloud_zones(self) -> list:
        """Get all cloud zones."""
        url = self._api_url("/iaas/api/zones")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('content', [])
            return []
        except:
            return []

    def get_network_profiles(self) -> list:
        """Get all network profiles."""
        url = self._api_url("/iaas/api/network-profiles")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('content', [])
            return []
        except:
            return []

    def get_storage_profiles(self) -> list:
        """Get all storage profiles."""
        url = self._api_url("/iaas/api/storage-profiles")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('content', [])
            return []
        except:
            return []

    def get_storage_profile_detail(self, profile_id: str) -> dict:
        """Get full details of a single storage profile."""
        url = self._api_url(f"/iaas/api/storage-profiles/{profile_id}")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            return {}
        except:
            return {}

    def get_fabric_vsphere_datastores(self) -> list:
        """Get all vSphere fabric datastores."""
        url = self._api_url("/iaas/api/fabric-vsphere-datastores")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('content', [])
            return []
        except:
            return []

    def create_storage_profile(self, name: str, description: str, region_id: str, 
                               tags: list, provisioning_type: str = "thin",
                               default_item: bool = False, compute_host_id: str = None,
                               verbose: bool = False) -> tuple:
        """
        Create a new vSphere storage profile.
        
        Uses "Datastore default" policy (no specific datastore/policy required).
        
        Args:
            name: Storage profile name
            description: Description
            region_id: Region ID
            tags: List of tags
            provisioning_type: thin or thick
            default_item: Whether this is the default profile
            compute_host_id: Optional compute cluster ID to bind to
            verbose: Show debug output
        """
        url = self._api_url("/iaas/api/storage-profiles")
        
        payload = {
            "name": name,
            "description": description,
            "defaultItem": default_item,
            "supportsEncryption": False,
            "tags": [{"key": t['key'], "value": t['value']} for t in tags],
            "diskProperties": {
                "provisioningType": provisioning_type
            },
            "regionId": region_id
        }
        
        # Add compute binding if specified
        if compute_host_id:
            payload["computeHostId"] = compute_host_id
        
        if verbose:
            print(f"    [DEBUG] POST {url}")
            print(f"    [DEBUG] Payload: {payload}")
        
        try:
            resp = self.session.post(url, json=payload, timeout=60)
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
                if resp.status_code not in [200, 201]:
                    print(f"    [DEBUG] Response: {resp.text[:500]}")
            
            if resp.status_code in [200, 201]:
                return True, resp.json()
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)

    def get_fabric_computes(self) -> list:
        """Get all fabric computes (clusters, resource pools, etc.)."""
        url = self._api_url("/iaas/api/fabric-computes")
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('content', [])
            return []
        except:
            return []

    def update_cloud_zone_tags(self, zone_id: str, tags: list, existing_zone: dict, verbose: bool = False) -> tuple:
        """
        Update capability tags on a cloud zone.
        
        Args:
            zone_id: Cloud zone ID
            tags: List of dicts with 'key' and 'value'
            existing_zone: The existing zone data (needed for required fields)
            verbose: Show debug output
        """
        url = self._api_url(f"/iaas/api/zones/{zone_id}")
        
        # Cloud zones require 'name' in the payload
        payload = {
            "name": existing_zone.get('name'),
            "tags": [{"key": t['key'], "value": t['value']} for t in tags]
        }
        
        if verbose:
            print(f"    [DEBUG] PATCH {url}")
            print(f"    [DEBUG] Payload: {payload}")
        
        try:
            resp = self.session.patch(url, json=payload, timeout=30)
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
                if resp.status_code not in [200, 204]:
                    print(f"    [DEBUG] Response: {resp.text[:500]}")
            
            if resp.status_code in [200, 204]:
                return True, resp.json() if resp.status_code == 200 else {}
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)

    def update_network_profile_tags(self, profile_id: str, tags: list, verbose: bool = False) -> tuple:
        """Update capability tags on a network profile."""
        url = self._api_url(f"/iaas/api/network-profiles/{profile_id}")
        
        payload = {
            "tags": [{"key": t['key'], "value": t['value']} for t in tags]
        }
        
        if verbose:
            print(f"    [DEBUG] PATCH {url}")
            print(f"    [DEBUG] Tags: {tags}")
        
        try:
            resp = self.session.patch(url, json=payload, timeout=30)
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
            
            if resp.status_code in [200, 204]:
                return True, resp.json() if resp.status_code == 200 else {}
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)

    def update_storage_profile_tags(self, profile_id: str, tags: list, existing_profile: dict, 
                                     compute_host_id: str = None, verbose: bool = False) -> tuple:
        """
        Update capability tags on a storage profile.
        
        NOTE: Storage profiles require PUT (not PATCH) with full payload.
        We must include all existing properties plus the new tags.
        
        Args:
            profile_id: Storage profile ID
            tags: List of dicts with 'key' and 'value'
            existing_profile: The existing profile data
            compute_host_id: Optional compute cluster ID to bind the storage profile
            verbose: Show debug output
        """
        url = self._api_url(f"/iaas/api/storage-profiles/{profile_id}")
        
        # Extract regionId from _links if not directly available
        region_id = existing_profile.get('regionId', '')
        if not region_id:
            links = existing_profile.get('_links', {})
            region_href = links.get('region', {}).get('href', '')
            if '/regions/' in region_href:
                region_id = region_href.split('/regions/')[-1]
        
        # Build full payload from existing profile
        payload = {
            "name": existing_profile.get('name'),
            "description": existing_profile.get('description', ''),
            "defaultItem": existing_profile.get('defaultItem', False),
            "supportsEncryption": existing_profile.get('supportsEncryption', False),
            "tags": [{"key": t['key'], "value": t['value']} for t in tags],
            "regionId": region_id
        }
        
        # Include diskProperties if present
        if 'diskProperties' in existing_profile:
            payload['diskProperties'] = existing_profile['diskProperties']
        
        # Include diskTargetProperties if present
        if 'diskTargetProperties' in existing_profile:
            payload['diskTargetProperties'] = existing_profile['diskTargetProperties']
        
        # Set compute binding if provided, otherwise preserve existing
        if compute_host_id:
            payload['computeHostId'] = compute_host_id
        elif existing_profile.get('computeHostId'):
            payload['computeHostId'] = existing_profile['computeHostId']
        
        if verbose:
            print(f"    [DEBUG] PUT {url}")
            print(f"    [DEBUG] Tags: {tags}")
            print(f"    [DEBUG] RegionId: {region_id}")
            if compute_host_id:
                print(f"    [DEBUG] ComputeHostId: {compute_host_id}")
        
        try:
            resp = self.session.put(url, json=payload, timeout=30)
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
                if resp.status_code not in [200, 204]:
                    print(f"    [DEBUG] Response: {resp.text[:500]}")
            
            if resp.status_code in [200, 204]:
                return True, resp.json() if resp.status_code == 200 else {}
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)

    def update_fabric_compute_tags(self, compute_id: str, tags: list, verbose: bool = False) -> tuple:
        """Update capability tags on a fabric compute (cluster)."""
        url = self._api_url(f"/iaas/api/fabric-computes/{compute_id}")
        
        payload = {
            "tags": [{"key": t['key'], "value": t['value']} for t in tags]
        }
        
        if verbose:
            print(f"    [DEBUG] PATCH {url}")
            print(f"    [DEBUG] Tags: {tags}")
        
        try:
            resp = self.session.patch(url, json=payload, timeout=30)
            if verbose:
                print(f"    [DEBUG] Status: {resp.status_code}")
            
            if resp.status_code in [200, 204]:
                return True, resp.json() if resp.status_code == 200 else {}
            else:
                try:
                    error = resp.json().get('message', resp.text[:200])
                except:
                    error = resp.text[:200]
                return False, error
        except Exception as e:
            return False, str(e)


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

def cmd_list_regions(client: AriaClient, config: dict):
    """List all available regions."""
    print("\n" + "=" * 70)
    print("AVAILABLE REGIONS")
    print("=" * 70)
    
    regions = client.get_regions()
    if not regions:
        print("  No regions found")
        return
    
    print(f"\nFound {len(regions)} regions:\n")
    for r in regions:
        print(f"  Name: {r.get('name', 'N/A')}")
        print(f"    ID: {r.get('id', 'N/A')}")
        print(f"    External Region ID: {r.get('externalRegionId', 'N/A')}")
        print(f"    Cloud Account: {r.get('cloudAccountId', 'N/A')[:8]}...")
        print()


def cmd_list_flavors(client: AriaClient, config: dict):
    """List existing flavor profiles."""
    print("\n" + "=" * 70)
    print("EXISTING FLAVOR PROFILES")
    print("=" * 70)
    
    profiles = client.get_flavor_profiles()
    if not profiles:
        print("  No flavor profiles found")
        return
    
    # Group by name
    by_name = {}
    for p in profiles:
        name = p.get('name', 'Unknown')
        if name not in by_name:
            by_name[name] = []
        by_name[name].append(p)
    
    print(f"\nFound {len(profiles)} flavor profiles ({len(by_name)} unique names):\n")
    
    for name, items in sorted(by_name.items()):
        print(f"  {name} ({len(items)} region(s))")
        for item in items:
            mappings = item.get('flavorMappings', {}).get('mapping', {})
            flavor_def = mappings.get(name, {})
            cpu = flavor_def.get('cpuCount', '?')
            mem = flavor_def.get('memoryInMB', '?')
            region = item.get('externalRegionId', 'Unknown')
            print(f"    - {region}: {cpu} vCPU, {mem} MB")


def cmd_list_images(client: AriaClient, config: dict):
    """List available fabric images (vCenter templates)."""
    print("\n" + "=" * 70)
    print("AVAILABLE FABRIC IMAGES (vCenter Templates)")
    print("=" * 70)
    
    images = client.get_fabric_images()
    if not images:
        print("  No fabric images found")
        return
    
    # Group by region
    by_region = {}
    for img in images:
        region = img.get('externalRegionId', 'Unknown')
        if region not in by_region:
            by_region[region] = []
        by_region[region].append(img)
    
    print(f"\nFound {len(images)} templates across {len(by_region)} regions:\n")
    
    for region, items in sorted(by_region.items()):
        print(f"  Region: {region}")
        for item in sorted(items, key=lambda x: x.get('name', '')):
            name = item.get('name', 'Unknown')
            os_family = item.get('osFamily', '')
            print(f"    - {name}" + (f" ({os_family})" if os_family else ""))
        print()


def resolve_regions(client: AriaClient, config: dict) -> dict:
    """
    Resolve region names from config to region IDs.
    
    Returns: dict mapping region name -> region ID
    """
    api_regions = client.get_regions()
    region_lookup = {r.get('name', ''): r.get('id') for r in api_regions}
    
    resolved = {}
    config_regions = config.get('regions', [])
    
    for cr in config_regions:
        name = cr.get('name')
        if name in region_lookup:
            resolved[name] = region_lookup[name]
        else:
            print(f"  ⚠ Region not found: {name}")
            print(f"    Available regions: {list(region_lookup.keys())}")
    
    return resolved


def cmd_process_flavors(client: AriaClient, config: dict, dry_run: bool, verbose: bool = False):
    """
    Process flavor mappings from config.
    
    CRITICAL: The API expects ALL flavors in a SINGLE call per region.
    We make 1 API call per region, each containing all flavor definitions.
    """
    print("\n" + "=" * 70)
    print("FLAVOR MAPPING AUTOMATION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    if verbose:
        print("Verbose: ON")
    print()
    
    # Resolve regions
    print("[1/3] Resolving regions...")
    resolved_regions = resolve_regions(client, config)
    if not resolved_regions:
        print("  ✗ No valid regions found!")
        return 1
    
    for name, rid in resolved_regions.items():
        print(f"    ✓ {name}: {rid[:8]}...")
    
    # Get flavors from config
    print("\n[2/3] Loading flavors from config...")
    flavors = config.get('flavors', [])
    if not flavors:
        print("  ✗ No flavors defined in config!")
        return 1
    
    print(f"  ✓ Found {len(flavors)} flavor definitions")
    for f in flavors:
        print(f"    - {f['name']}: {f['cpuCount']} vCPU, {f['memoryMB'] // 1024} GB")
    
    # Process - one API call per region with ALL flavors
    print(f"\n[3/3] {'Previewing' if dry_run else 'Creating'} flavor profiles...")
    print(f"  Strategy: 1 API call per region × {len(resolved_regions)} regions")
    print(f"  Each call contains ALL {len(flavors)} flavors")
    print("=" * 70)
    
    profile_name = config.get('flavor_profile_name', 'vSphere-flavor-profile')
    profile_desc = config.get('flavor_profile_description', 
                              f'Flavor profile with {len(flavors)} size options')
    
    if dry_run:
        print("\nWould CREATE flavor profiles:")
        for region_name, region_id in resolved_regions.items():
            print(f"\n  Profile for {region_name}:")
            print(f"    Name: {profile_name}")
            print(f"    Region ID: {region_id}")
            print(f"    Flavors: {len(flavors)}")
            for f in flavors:
                print(f"      - {f['name']}: {f['cpuCount']} vCPU, {f['memoryMB']} MB")
    else:
        created = 0
        failed = 0
        
        for region_name, region_id in resolved_regions.items():
            print(f"\n  Creating profile for {region_name}...")
            
            success, result = client.create_flavor_profile(
                profile_name=profile_name,
                description=profile_desc,
                region_id=region_id,
                flavors=flavors,
                verbose=verbose
            )
            
            if success:
                created += 1
                result_id = result.get('id', 'unknown') if isinstance(result, dict) else 'unknown'
                print(f"  ✓ Created: {profile_name} @ {region_name}")
                print(f"    Profile ID: {result_id[:40]}...")
                print(f"    Contains {len(flavors)} flavor mappings")
            else:
                failed += 1
                print(f"  ✗ Failed: {region_name}: {result}")
        
        print(f"\n  Profiles created: {created}")
        if failed:
            print(f"  Profiles failed: {failed}")
        
        # Verification step
        if created > 0:
            print("\n[VERIFICATION] Checking what actually exists in Aria...")
            actual = client.get_flavor_profiles()
            print(f"  Total flavor profiles: {len(actual)}")
            
            # Show details
            for p in actual:
                name = p.get('name', 'Unknown')
                mappings = p.get('flavorMappings', {}).get('mapping', {})
                region = p.get('externalRegionId', 'Unknown')
                print(f"    - {name} @ {region}: {len(mappings)} flavors")
                if verbose:
                    for fname, fdef in mappings.items():
                        print(f"        {fname}: {fdef.get('cpuCount')} vCPU, {fdef.get('memoryInMB')} MB")
    
    print("\nDONE")
    return 0


def cmd_process_images(client: AriaClient, config: dict, dry_run: bool, verbose: bool = False):
    """
    Process image mappings from config.
    
    CRITICAL: The API expects fabric image 'id', not template name.
    We must look up the ID from /iaas/api/fabric-images first.
    """
    print("\n" + "=" * 70)
    print("IMAGE MAPPING AUTOMATION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    if verbose:
        print("Verbose: ON")
    print()
    
    # Resolve regions
    print("[1/4] Resolving regions...")
    resolved_regions = resolve_regions(client, config)
    if not resolved_regions:
        print("  ✗ No valid regions found!")
        return 1
    
    for name, rid in resolved_regions.items():
        print(f"    ✓ {name}: {rid[:8]}...")
    
    # Fetch fabric images and build lookup
    print("\n[2/4] Fetching fabric images (vCenter templates)...")
    fabric_images = client.get_fabric_images()
    
    # Build lookup: (region_id, template_name) -> fabric_image_id
    fabric_lookup = {}
    for img in fabric_images:
        name = img.get('name', '')
        img_id = img.get('id', '')
        links = img.get('_links', {})
        region_href = links.get('region', {}).get('href', '')
        region_id = ''
        if '/regions/' in region_href:
            region_id = region_href.split('/regions/')[-1]
        
        if name and img_id and region_id:
            fabric_lookup[(region_id, name)] = img_id
    
    print(f"  ✓ Found {len(fabric_images)} fabric images")
    print(f"  ✓ Built lookup with {len(fabric_lookup)} entries")
    
    # Get images from config and resolve to fabric IDs
    print("\n[3/4] Loading images from config and resolving IDs...")
    images_config = config.get('images', [])
    if not images_config:
        print("  ✗ No images defined in config!")
        return 1
    
    # Build image list per region with resolved IDs
    images_by_region = {region_name: [] for region_name in resolved_regions.keys()}
    warnings = []
    
    for img in images_config:
        mapping_name = img.get('name')
        templates = img.get('templates', {})
        
        for region_name, template_name in templates.items():
            region_id = resolved_regions.get(region_name)
            if not region_id:
                warnings.append(f"Region '{region_name}' not found for image '{mapping_name}'")
                continue
            
            # Look up the fabric image ID
            fabric_id = fabric_lookup.get((region_id, template_name))
            
            if not fabric_id:
                warnings.append(f"Template '{template_name}' not found in region '{region_name}' for '{mapping_name}'")
                continue
            
            images_by_region[region_name].append({
                'name': mapping_name,
                'template': template_name,
                'id': fabric_id
            })
    
    if warnings:
        print("\n  Warnings:")
        for w in warnings:
            print(f"    ⚠ {w}")
    
    for region_name, images in images_by_region.items():
        print(f"\n  {region_name}: {len(images)} images resolved")
        for img in images:
            print(f"    - {img['name']} → {img['template']} (id: {img['id'][:16]}...)")
    
    # Process - one API call per region with ALL images
    print(f"\n[4/4] {'Previewing' if dry_run else 'Creating'} image profiles...")
    print("=" * 70)
    
    profile_name = config.get('image_profile_name', 'vSphere-image-profile')
    profile_desc = config.get('image_profile_description', 'Image profile for VM deployments')
    
    if dry_run:
        print("\nWould CREATE image profiles:")
        for region_name, images in images_by_region.items():
            if images:
                print(f"\n  Profile for {region_name}:")
                print(f"    Name: {profile_name}")
                print(f"    Images: {len(images)}")
                for img in images:
                    print(f"      - {img['name']} → {img['template']} (id: {img['id'][:16]}...)")
    else:
        created = 0
        failed = 0
        
        for region_name, images in images_by_region.items():
            if not images:
                print(f"\n  Skipping {region_name} - no images resolved")
                continue
                
            region_id = resolved_regions[region_name]
            print(f"\n  Creating profile for {region_name}...")
            
            success, result = client.create_image_profile(
                profile_name=profile_name,
                description=profile_desc,
                region_id=region_id,
                images=images,
                verbose=verbose
            )
            
            if success:
                created += 1
                result_id = result.get('id', 'unknown') if isinstance(result, dict) else 'unknown'
                print(f"  ✓ Created: {profile_name} @ {region_name}")
                print(f"    Profile ID: {result_id[:40]}...")
                print(f"    Contains {len(images)} image mappings")
            else:
                failed += 1
                print(f"  ✗ Failed: {region_name}: {result}")
        
        print(f"\n  Profiles created: {created}")
        if failed:
            print(f"  Profiles failed: {failed}")
        
        # Verification step
        if created > 0:
            print("\n[VERIFICATION] Checking what actually exists in Aria...")
            actual = client.get_image_profiles()
            print(f"  Total image profiles: {len(actual)}")
            
            for p in actual:
                name = p.get('name', 'Unknown')
                mappings = p.get('imageMappings', {}).get('mapping', {})
                region = p.get('externalRegionId', 'Unknown')
                print(f"    - {name} @ {region}: {len(mappings)} images")
                if verbose:
                    for iname, idef in mappings.items():
                        img_id = idef.get('id', 'N/A')
                        print(f"        {iname} → id: {img_id[:20] if img_id != 'N/A' else 'N/A'}...")
    
    print("\nDONE")
    return 0


# =============================================================================
# TAG COMMAND HANDLERS
# =============================================================================

def cmd_list_tags(client: AriaClient, config: dict):
    """List current tags on cloud zones, network profiles, storage profiles, and computes."""
    print("\n" + "=" * 70)
    print("CURRENT TAGS IN ARIA")
    print("=" * 70)
    
    # Cloud Zones
    print("\n[Cloud Zones]")
    zones = client.get_cloud_zones()
    if zones:
        for z in zones:
            name = z.get('name', 'Unknown')
            tags = z.get('tags', [])
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags]) if tags else "(no tags)"
            print(f"  {name}: {tag_str}")
    else:
        print("  No cloud zones found")
    
    # Network Profiles
    print("\n[Network Profiles]")
    profiles = client.get_network_profiles()
    if profiles:
        for p in profiles:
            name = p.get('name', 'Unknown')
            tags = p.get('tags', [])
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags]) if tags else "(no tags)"
            print(f"  {name}: {tag_str}")
    else:
        print("  No network profiles found")
    
    # Storage Profiles (brief - use --list-storage for details)
    print("\n[Storage Profiles] (use --list-storage for details)")
    storage = client.get_storage_profiles()
    if storage:
        for s in storage:
            name = s.get('name', 'Unknown')
            tags = s.get('tags', [])
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags]) if tags else "(no tags)"
            print(f"  {name}: {tag_str}")
    else:
        print("  No storage profiles found")
    
    # Fabric Computes (Clusters) - only show first 20
    print("\n[Fabric Computes (Clusters)] - showing first 20")
    computes = client.get_fabric_computes()
    if computes:
        for c in computes[:20]:
            name = c.get('name', 'Unknown')
            tags = c.get('tags', [])
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags]) if tags else "(no tags)"
            print(f"  {name}: {tag_str}")
        if len(computes) > 20:
            print(f"  ... and {len(computes) - 20} more")
    else:
        print("  No fabric computes found")


def cmd_list_storage(client: AriaClient, config: dict):
    """List existing storage profiles with details."""
    print("\n" + "=" * 70)
    print("EXISTING STORAGE PROFILES")
    print("=" * 70)
    
    storage = client.get_storage_profiles()
    if not storage:
        print("  No storage profiles found")
        return
    
    # Group by region
    by_region = {}
    for s in storage:
        # Get region from _links
        links = s.get('_links', {})
        region_href = links.get('region', {}).get('href', '')
        region_name = s.get('externalRegionId', 'Unknown')
        
        if region_name not in by_region:
            by_region[region_name] = []
        by_region[region_name].append(s)
    
    print(f"\nFound {len(storage)} storage profiles in {len(by_region)} regions:\n")
    
    for region, profiles in sorted(by_region.items()):
        print(f"[{region}]")
        for p in profiles:
            name = p.get('name', 'Unknown')
            tags = p.get('tags', [])
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags]) if tags else "(no tags)"
            default = "✓ DEFAULT" if p.get('defaultItem') else ""
            disk_props = p.get('diskProperties', {})
            prov_type = disk_props.get('provisioningType', 'N/A')
            
            print(f"  {name}")
            print(f"    Tags: {tag_str}")
            print(f"    Provisioning: {prov_type} {default}")
        print()


def cmd_process_tags(client: AriaClient, config: dict, dry_run: bool, verbose: bool = False):
    """Process tag configurations from config."""
    print("\n" + "=" * 70)
    print("TAG AUTOMATION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    if verbose:
        print("Verbose: ON")
    print()
    
    tags_config = config.get('tags', {})
    if not tags_config:
        print("  ✗ No tags defined in config!")
        return 1
    
    results = {'updated': 0, 'failed': 0, 'skipped': 0}
    
    # Process Cloud Zones (with idempotency)
    cz_config = tags_config.get('cloud_zones', [])
    if cz_config:
        print("[1] Processing Cloud Zone tags...")
        zones = client.get_cloud_zones()
        zone_lookup = {z.get('name'): z for z in zones}
        
        for cz in cz_config:
            name = cz.get('name')
            tags = cz.get('tags', [])
            
            zone = zone_lookup.get(name)
            if not zone:
                print(f"  ⚠ Cloud Zone '{name}' not found")
                results['skipped'] += 1
                continue
            
            # IDEMPOTENCY: Check if tags already match
            existing_tags = zone.get('tags', [])
            existing_tag_set = set(f"{t['key']}:{t['value']}" for t in existing_tags)
            desired_tag_set = set(f"{t['key']}:{t['value']}" for t in tags)
            
            if existing_tag_set == desired_tag_set:
                print(f"  ✓ '{name}' already has correct tags (no change)")
                continue
            
            zone_id = zone.get('id')
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags])
            
            if dry_run:
                print(f"  Would update '{name}' with tags: {tag_str}")
            else:
                success, result = client.update_cloud_zone_tags(zone_id, tags, zone, verbose)
                if success:
                    print(f"  ✓ Updated '{name}' with tags: {tag_str}")
                    results['updated'] += 1
                else:
                    print(f"  ✗ Failed '{name}': {result}")
                    results['failed'] += 1
    
    # Process Network Profiles (with idempotency)
    np_config = tags_config.get('network_profiles', [])
    if np_config:
        print("\n[2] Processing Network Profile tags...")
        profiles = client.get_network_profiles()
        profile_lookup = {p.get('name'): p for p in profiles}
        
        for np in np_config:
            name = np.get('name')
            tags = np.get('tags', [])
            
            profile = profile_lookup.get(name)
            if not profile:
                print(f"  ⚠ Network Profile '{name}' not found")
                results['skipped'] += 1
                continue
            
            # IDEMPOTENCY: Check if tags already match
            existing_tags = profile.get('tags', [])
            existing_tag_set = set(f"{t['key']}:{t['value']}" for t in existing_tags)
            desired_tag_set = set(f"{t['key']}:{t['value']}" for t in tags)
            
            if existing_tag_set == desired_tag_set:
                print(f"  ✓ '{name}' already has correct tags (no change)")
                continue
            
            profile_id = profile.get('id')
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags])
            
            if dry_run:
                print(f"  Would update '{name}' with tags: {tag_str}")
            else:
                success, result = client.update_network_profile_tags(profile_id, tags, verbose)
                if success:
                    print(f"  ✓ Updated '{name}' with tags: {tag_str}")
                    results['updated'] += 1
                else:
                    print(f"  ✗ Failed '{name}': {result}")
                    results['failed'] += 1
    
    # Process Compute (Clusters) with idempotency
    compute_config = tags_config.get('compute', [])
    if compute_config:
        print("\n[3] Processing Compute (Cluster) tags...")
        computes = client.get_fabric_computes()
        compute_lookup = {c.get('name'): c for c in computes}
        
        for cc in compute_config:
            name = cc.get('name')
            tags = cc.get('tags', [])
            
            compute = compute_lookup.get(name)
            if not compute:
                print(f"  ⚠ Compute '{name}' not found")
                results['skipped'] += 1
                continue
            
            # IDEMPOTENCY: Check if tags already match
            existing_tags = compute.get('tags', [])
            existing_tag_set = set(f"{t['key']}:{t['value']}" for t in existing_tags)
            desired_tag_set = set(f"{t['key']}:{t['value']}" for t in tags)
            
            if existing_tag_set == desired_tag_set:
                print(f"  ✓ '{name}' already has correct tags (no change)")
                continue
            
            compute_id = compute.get('id')
            tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags])
            
            if dry_run:
                print(f"  Would update '{name}' with tags: {tag_str}")
            else:
                success, result = client.update_fabric_compute_tags(compute_id, tags, verbose)
                if success:
                    print(f"  ✓ Updated '{name}' with tags: {tag_str}")
                    results['updated'] += 1
                else:
                    print(f"  ✗ Failed '{name}': {result}")
                    results['failed'] += 1
    
    # Summary
    print("\n" + "=" * 70)
    if dry_run:
        print("DRY RUN COMPLETE - No changes made")
    else:
        print(f"SUMMARY: Updated={results['updated']}, Failed={results['failed']}, Skipped={results['skipped']}")
    
    print("\nDONE")
    return 0


def cmd_process_storage(client: AriaClient, config: dict, dry_run: bool, verbose: bool = False):
    """Process storage profiles from config - create new and update existing."""
    print("\n" + "=" * 70)
    print("STORAGE PROFILE AUTOMATION")
    print("=" * 70)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
    if verbose:
        print("Verbose: ON")
    print()
    
    tags_config = config.get('tags', {})
    sp_config = tags_config.get('storage_profiles', [])
    
    if not sp_config:
        print("  ✗ No storage_profiles defined in config!")
        return 1
    
    results = {'created': 0, 'updated': 0, 'failed': 0, 'skipped': 0}
    
    # Get existing storage profiles
    print("[1/3] Fetching existing storage profiles...")
    storage = client.get_storage_profiles()
    storage_lookup = {s.get('name'): s for s in storage}
    print(f"  Found {len(storage)} existing profiles")
    
    # Get regions for creating new profiles
    regions = client.get_regions()
    region_lookup = {r.get('name'): r.get('id') for r in regions}
    
    # Get compute clusters for binding
    print("[2/3] Fetching compute clusters for binding...")
    computes = client.get_fabric_computes()
    compute_lookup = {c.get('name'): c.get('id') for c in computes}
    print(f"  Found {len(computes)} compute clusters")
    
    print(f"\n[3/3] Processing {len(sp_config)} storage profile configurations...")
    
    for sp in sp_config:
        name = sp.get('name')
        tags = sp.get('tags', [])
        should_create = sp.get('create', False)
        compute_name = sp.get('compute')  # Optional compute cluster binding
        tag_str = ', '.join([f"{t['key']}:{t['value']}" for t in tags])
        
        # Resolve compute ID if specified
        compute_id = None
        if compute_name:
            compute_id = compute_lookup.get(compute_name)
            if not compute_id:
                print(f"  ⚠ Compute '{compute_name}' not found for '{name}'")
        
        profile_summary = storage_lookup.get(name)
        
        # IDEMPOTENCY: Profile already exists
        if profile_summary:
            profile = client.get_storage_profile_detail(profile_summary.get('id'))
            if not profile:
                print(f"  ✗ Failed '{name}': Could not fetch profile details")
                results['failed'] += 1
                continue
            
            # Check if tags match
            existing_tags = profile.get('tags', [])
            existing_tag_set = set(f"{t['key']}:{t['value']}" for t in existing_tags)
            desired_tag_set = set(f"{t['key']}:{t['value']}" for t in tags)
            
            # Check if compute binding matches
            existing_compute = profile.get('computeHostId')
            compute_matches = (not compute_id) or (existing_compute == compute_id)
            
            if existing_tag_set == desired_tag_set and compute_matches:
                compute_info = f" (compute: {compute_name})" if compute_name else ""
                print(f"  ✓ '{name}' already configured correctly{compute_info} (no change)")
                continue
            
            # Something differs - update
            profile_id = profile_summary.get('id')
            changes = []
            if existing_tag_set != desired_tag_set:
                changes.append(f"tags: {tag_str}")
            if not compute_matches:
                changes.append(f"compute: {compute_name}")
            
            if dry_run:
                print(f"  Would UPDATE '{name}': {', '.join(changes)}")
            else:
                success, result = client.update_storage_profile_tags(
                    profile_id, tags, profile, compute_id, verbose
                )
                if success:
                    print(f"  ✓ Updated '{name}': {', '.join(changes)}")
                    results['updated'] += 1
                else:
                    print(f"  ✗ Failed '{name}': {result}")
                    results['failed'] += 1
        
        # Profile doesn't exist - create if requested
        elif should_create:
            region_name = sp.get('region')
            description = sp.get('description', f'{name} storage profile')
            provisioning_type = sp.get('provisioning_type', 'thin')
            default_item = sp.get('default', False)
            
            # Resolve region ID
            region_id = region_lookup.get(region_name)
            if not region_id:
                print(f"  ⚠ Cannot create '{name}': Region '{region_name}' not found")
                results['skipped'] += 1
                continue
            
            compute_info = f" bound to {compute_name}" if compute_name else ""
            
            if dry_run:
                print(f"  Would CREATE '{name}' in {region_name}{compute_info} with tags: {tag_str}")
            else:
                success, result = client.create_storage_profile(
                    name=name,
                    description=description,
                    region_id=region_id,
                    tags=tags,
                    provisioning_type=provisioning_type,
                    default_item=default_item,
                    compute_host_id=compute_id,
                    verbose=verbose
                )
                if success:
                    print(f"  ✓ Created '{name}' in {region_name}{compute_info} with tags: {tag_str}")
                    results['created'] += 1
                else:
                    print(f"  ✗ Failed to create '{name}': {result}")
                    results['failed'] += 1
        
        # Profile doesn't exist and create not requested
        else:
            print(f"  ⚠ '{name}' not found (use 'create: true' in config to create)")
            results['skipped'] += 1
    
    # Summary
    print("\n" + "=" * 70)
    if dry_run:
        print("DRY RUN COMPLETE - No changes made")
    else:
        print(f"SUMMARY: Created={results['created']}, Updated={results['updated']}, Failed={results['failed']}, Skipped={results['skipped']}")
    
    print("\nDONE")
    return 0


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='VMware Aria Automation - Infrastructure Mapping Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
================================================================================
SELECTIVE PROCESSING - Troubleshoot one component at a time
================================================================================

LIST COMMANDS (read-only, safe to run anytime):
  --list-regions    Show available cloud regions
  --list-flavors    Show existing flavor profiles  
  --list-images     Show vCenter templates available for image mappings
  --list-storage    Show storage profiles with tags and provisioning details
  --list-tags       Show capability tags on cloud zones, network profiles, computes

PROCESS COMMANDS (require --dry-run or --execute):
  --flavors         Process flavor profiles from config
  --images          Process image profiles from config
  --storage         Process storage profiles (create new, update existing)
  --tags            Process capability tags (cloud zones, network profiles, computes)
  --all             Process ALL components at once

EXAMPLES:
  # Step 1: Discover what exists
  python3 aria_mapping.py --config config.yaml --list-regions
  python3 aria_mapping.py --config config.yaml --list-storage
  python3 aria_mapping.py --config config.yaml --list-tags

  # Step 2: Preview changes (always do this first!)
  python3 aria_mapping.py --config config.yaml --flavors --dry-run
  python3 aria_mapping.py --config config.yaml --images --dry-run
  python3 aria_mapping.py --config config.yaml --storage --dry-run
  python3 aria_mapping.py --config config.yaml --tags --dry-run

  # Step 3: Execute one component at a time
  python3 aria_mapping.py --config config.yaml --flavors --execute
  python3 aria_mapping.py --config config.yaml --images --execute
  python3 aria_mapping.py --config config.yaml --storage --execute
  python3 aria_mapping.py --config config.yaml --tags --execute

  # Or execute everything at once
  python3 aria_mapping.py --config config.yaml --all --execute

  # Troubleshooting with verbose mode
  python3 aria_mapping.py --config config.yaml --storage --execute --verbose

IDEMPOTENCY:
  All commands are idempotent - safe to run multiple times.
  Resources with matching configuration show "no change" and are skipped.

================================================================================
        """
    )
    
    parser.add_argument('--config', required=True, help='YAML configuration file')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    
    # List commands
    list_group = parser.add_argument_group('List Commands (no changes made)')
    list_group.add_argument('--list-regions', action='store_true', 
                           help='List available cloud regions')
    list_group.add_argument('--list-flavors', action='store_true',
                           help='List existing flavor profiles')
    list_group.add_argument('--list-images', action='store_true',
                           help='List available fabric images (vCenter templates)')
    list_group.add_argument('--list-storage', action='store_true',
                           help='List existing storage profiles')
    list_group.add_argument('--list-tags', action='store_true',
                           help='List current tags on cloud zones, network/storage profiles, computes')
    
    # Process commands
    process_group = parser.add_argument_group('Process Commands (require --dry-run or --execute)')
    process_group.add_argument('--flavors', action='store_true',
                              help='Process flavor profiles from config')
    process_group.add_argument('--images', action='store_true',
                              help='Process image profiles from config')
    process_group.add_argument('--storage', action='store_true',
                              help='Process storage profiles from config (create/update)')
    process_group.add_argument('--tags', action='store_true',
                              help='Process capability tags (cloud zones, network profiles, computes)')
    process_group.add_argument('--all', action='store_true',
                              help='Process ALL: flavors, images, storage, and tags')
    
    # Execution mode
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--dry-run', action='store_true',
                           help='Preview changes without executing')
    mode_group.add_argument('--execute', action='store_true',
                           help='Execute changes')
    
    # Debug mode
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Show detailed API requests and responses')
    
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Initialize client
    aria_config = config.get('aria', {})
    client = AriaClient(
        host=aria_config.get('host'),
        username=aria_config.get('username'),
        password=aria_config.get('password'),
        domain=aria_config.get('domain', 'System Domain'),
        verify_ssl=aria_config.get('verify_ssl', False)
    )
    
    # Authenticate
    print("=" * 70)
    print(f"ARIA MAPPING TOOL v{__version__}")
    print("=" * 70)
    print(f"\nConnecting to: {aria_config.get('host')}")
    print("Authenticating...", end=" ")
    
    if not client.authenticate():
        print("FAILED")
        return 1
    print("OK")
    
    # Execute commands
    if args.list_regions:
        cmd_list_regions(client, config)
        return 0
    
    if args.list_flavors:
        cmd_list_flavors(client, config)
        return 0
    
    if args.list_images:
        cmd_list_images(client, config)
        return 0
    
    if args.list_storage:
        cmd_list_storage(client, config)
        return 0
    
    if args.list_tags:
        cmd_list_tags(client, config)
        return 0
    
    # For process commands, require --dry-run or --execute
    if args.flavors or args.images or args.storage or args.tags or args.all:
        if not args.dry_run and not args.execute:
            parser.error("Specify --dry-run or --execute with --flavors/--images/--storage/--tags/--all")
        
        dry_run = args.dry_run
        
        if args.flavors or args.all:
            result = cmd_process_flavors(client, config, dry_run, verbose=args.verbose)
            if result != 0:
                return result
        
        if args.images or args.all:
            result = cmd_process_images(client, config, dry_run, verbose=args.verbose)
            if result != 0:
                return result
        
        if args.storage or args.all:
            result = cmd_process_storage(client, config, dry_run, verbose=args.verbose)
            if result != 0:
                return result
        
        if args.tags or args.all:
            result = cmd_process_tags(client, config, dry_run, verbose=args.verbose)
            if result != 0:
                return result
        
        return 0
    
    # No command specified
    parser.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
