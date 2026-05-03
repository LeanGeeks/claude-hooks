#!/usr/bin/env python3
"""
Settings Loader for Claude Code PreToolUse Hook

Loads and merges settings from:
1. Global: ~/.claude/settings.json
2. Workspace: .claude/settings.json
3. Local: .claude/settings.local.json

Handles both legacy (allowedTools/disallowedTools) and modern (permissions.allow/deny) formats.
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Any


class SettingsLoader:
    """Load and merge Claude Code settings from multiple locations"""

    # Class-level cache with TTL
    _cache: Dict[str, tuple[float, Dict]] = {}
    _cache_ttl: int = 60  # seconds

    def __init__(self, workspace_dir: Optional[str] = None):
        """
        Initialize settings loader

        Args:
            workspace_dir: Workspace directory (defaults to current directory)
        """
        self.workspace_dir = workspace_dir or os.getcwd()
        self.global_settings_path = os.path.expanduser("~/.claude/settings.json")
        self.workspace_settings_path = os.path.join(self.workspace_dir, ".claude/settings.json")
        self.local_settings_path = os.path.join(self.workspace_dir, ".claude/settings.local.json")

    def load_all_settings(self) -> Dict[str, Any]:
        """
        Load and merge settings from all three locations with caching

        Returns:
            Merged settings in modern format
        """
        # Check cache
        cache_key = self.workspace_dir
        now = time.time()

        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data

        # Load and merge
        settings = self._load_and_merge()

        # Cache result
        self._cache[cache_key] = (now, settings)

        return settings

    def _load_and_merge(self) -> Dict[str, Any]:
        """Load and merge settings from all locations"""
        # Load each file
        global_settings = self._load_json_file(self.global_settings_path)
        workspace_settings = self._load_json_file(self.workspace_settings_path)
        local_settings = self._load_json_file(self.local_settings_path)

        # Normalize each to modern format
        global_norm = self._normalize_to_modern_format(global_settings)
        workspace_norm = self._normalize_to_modern_format(workspace_settings)
        local_norm = self._normalize_to_modern_format(local_settings)

        # Merge in order: global → workspace → local
        result = self._merge_settings(global_norm, workspace_norm)
        result = self._merge_settings(result, local_norm)

        return result

    def _load_json_file(self, path: str) -> Dict[str, Any]:
        """
        Safely load JSON file with error handling

        Args:
            path: Path to JSON file

        Returns:
            Parsed JSON as dict, or empty dict if file doesn't exist or is invalid
        """
        try:
            if not os.path.exists(path):
                return {}

            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            # Log error to stderr but continue
            print(f"Warning: Failed to load {path}: {e}", file=__import__('sys').stderr)
            return {}

    def _normalize_to_modern_format(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert legacy allowedTools/disallowedTools to permissions.allow/deny

        Args:
            settings: Settings dict (may be legacy or modern format)

        Returns:
            Settings in modern format with permissions.allow and permissions.deny
        """
        result = {
            'permissions': {
                'allow': [],
                'deny': []
            }
        }

        # Handle modern format
        if 'permissions' in settings:
            if 'allow' in settings['permissions']:
                result['permissions']['allow'].extend(settings['permissions']['allow'])
            if 'deny' in settings['permissions']:
                result['permissions']['deny'].extend(settings['permissions']['deny'])

        # Handle legacy format
        if 'allowedTools' in settings:
            result['permissions']['allow'].extend(settings['allowedTools'])

        if 'disallowedTools' in settings:
            result['permissions']['deny'].extend(settings['disallowedTools'])

        # Remove duplicates while preserving order
        result['permissions']['allow'] = self._unique_ordered(result['permissions']['allow'])
        result['permissions']['deny'] = self._unique_ordered(result['permissions']['deny'])

        return result

    def _merge_settings(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deep merge override settings into base

        Args:
            base: Base settings
            override: Settings to merge in (takes precedence)

        Returns:
            Merged settings
        """
        result = {
            'permissions': {
                'allow': [],
                'deny': []
            }
        }

        # Merge allow lists (base first, then override)
        base_allow = base.get('permissions', {}).get('allow', [])
        override_allow = override.get('permissions', {}).get('allow', [])
        result['permissions']['allow'] = base_allow + override_allow

        # Merge deny lists (base first, then override)
        base_deny = base.get('permissions', {}).get('deny', [])
        override_deny = override.get('permissions', {}).get('deny', [])
        result['permissions']['deny'] = base_deny + override_deny

        # Remove duplicates while preserving order (later occurrences override earlier)
        result['permissions']['allow'] = self._unique_ordered(result['permissions']['allow'])
        result['permissions']['deny'] = self._unique_ordered(result['permissions']['deny'])

        return result

    @staticmethod
    def _unique_ordered(items: List[str]) -> List[str]:
        """
        Remove duplicates from list while preserving order

        Args:
            items: List of strings

        Returns:
            List with duplicates removed, preserving order
        """
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


# For testing
if __name__ == '__main__':
    import sys

    # Test settings loader
    loader = SettingsLoader()
    settings = loader.load_all_settings()

    print("=== Merged Settings ===")
    print(json.dumps(settings, indent=2))
    print(f"\nAllow rules: {len(settings['permissions']['allow'])}")
    print(f"Deny rules: {len(settings['permissions']['deny'])}")

    # Show sample rules
    print("\n=== Sample Allow Rules (first 10) ===")
    for rule in settings['permissions']['allow'][:10]:
        print(f"  {rule}")

    print("\n=== Sample Deny Rules (first 10) ===")
    for rule in settings['permissions']['deny'][:10]:
        print(f"  {rule}")
