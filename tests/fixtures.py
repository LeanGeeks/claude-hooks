#!/usr/bin/env python3
"""
Test fixtures for Task 04: Permission Flow Integration Tests

This module provides sample data for testing the permission flow:
- PreToolUse input payloads
- PermissionRequest payloads
- permission_suggestions samples
- Stale and duplicate callback samples
"""

import json
from typing import Dict, Any, List


# Sample PreToolUse input payloads
PRETOOL_USE_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "allowed_simple": {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "test-session-001",
        "cwd": "/home/user/project"
    },
    "allowed_compound": {
        "tool_name": "Bash",
        "tool_input": {"command": "git status && git diff"},
        "session_id": "test-session-002",
        "cwd": "/home/user/project"
    },
    "unknown_command": {
        "tool_name": "Bash",
        "tool_input": {"command": "some_unknown_command --flag"},
        "session_id": "test-session-003",
        "cwd": "/home/user/project"
    },
    "denied_command": {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "session_id": "test-session-004",
        "cwd": "/home/user/project"
    },
    "mixed_allowed_unknown": {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la && unknown_cmd"},
        "session_id": "test-session-005",
        "cwd": "/home/user/project"
    },
    "non_bash_tool": {
        "tool_name": "Read",
        "tool_input": {"file_path": "/home/user/project/file.txt"},
        "session_id": "test-session-006",
        "cwd": "/home/user/project"
    },
    "complex_pipeline": {
        "tool_name": "Bash",
        "tool_input": {"command": "git diff | head -100 | grep -i error"},
        "session_id": "test-session-007",
        "cwd": "/home/user/project"
    }
}


# Sample PermissionRequest input payloads
PERMISSION_REQUEST_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "basic_bash": {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /tmp/test"},
        "session_id": "test-session-perm-001",
        "cwd": "/home/user/project",
        "permission_suggestions": ["Bash(rm:*)"]
    },
    "with_suggestions": {
        "tool_name": "Bash",
        "tool_input": {"command": "npm install && npm run build"},
        "session_id": "test-session-perm-002",
        "cwd": "/home/user/project",
        "permission_suggestions": ["Bash(npm:*)"]
    },
    "denied_pattern": {
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force"},
        "session_id": "test-session-perm-003",
        "cwd": "/home/user/project",
        "permission_suggestions": []
    },
    "no_suggestions": {
        "tool_name": "Bash",
        "tool_input": {"command": "custom_script.sh"},
        "session_id": "test-session-perm-004",
        "cwd": "/home/user/project",
        "permission_suggestions": []
    }
}


# Sample permission_suggestions for different scenarios
PERMISSION_SUGGESTIONS: Dict[str, List[str]] = {
    "bash_ls": ["Bash(ls:*)"],
    "bash_git": ["Bash(git:*)"],
    "bash_npm": ["Bash(npm:*)"],
    "bash_custom": ["Bash(custom_script.sh:*)"],
    "bash_dangerous": ["Bash(rm:*)"],
    "multiple": ["Bash(git:*)", "Bash(npm:*)"],
    "empty": []
}


# Sample Telegram callback payloads
TELEGRAM_CALLBACKS: Dict[str, Dict[str, Any]] = {
    "allow": {
        "id": "callback-allow-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1001,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `abc123`\n\n```\nls -la\n```\n\nApprove this command?"
        },
        "data": "allow:abc123"
    },
    "deny": {
        "id": "callback-deny-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1002,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `def456`\n\n```\nrm -rf\n```\n\nApprove this command?"
        },
        "data": "deny:def456"
    },
    "stop": {
        "id": "callback-stop-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1003,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `ghi789`\n\n```\ndangerous_cmd\n```\n\nApprove this command?"
        },
        "data": "stop:ghi789"
    },
    "whitelist": {
        "id": "callback-whitelist-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1004,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `jkl012`\n\n```\ncustom_cmd\n```\n\nApprove this command?"
        },
        "data": "whitelist:jkl012"
    },
    "unauthorized_user": {
        "id": "callback-unauth-001",
        "from": {"id": 999999999, "is_bot": False, "first_name": "Attacker"},
        "message": {
            "message_id": 1005,
            "chat": {"id": 999999999, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `mno345`\n\n```\nls -la\n```\n\nApprove this command?"
        },
        "data": "allow:mno345"
    },
    "invalid_action": {
        "id": "callback-invalid-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1006,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `pqr678`\n\n```\nls -la\n```\n\nApprove this command?"
        },
        "data": "hack:pqr678"
    },
    "duplicate_allow": {
        "id": "callback-dup-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 1007,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `stu901`\n\n```\nls -la\n```\n\nApprove this command?"
        },
        "data": "allow:stu901"
    }
}


# Sample Telegram text reply payloads
TELEGRAM_TEXT_REPLIES: Dict[str, Dict[str, Any]] = {
    "reply_to_permission": {
        "message_id": 2001,
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "chat": {"id": 123456789, "type": "private"},
        "reply_to_message": {
            "message_id": 1001,
            "chat": {"id": 123456789, "type": "private"}
        },
        "text": "Please use a different approach"
    },
    "reply_short": {
        "message_id": 2002,
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "chat": {"id": 123456789, "type": "private"},
        "reply_to_message": {
            "message_id": 1002,
            "chat": {"id": 123456789, "type": "private"}
        },
        "text": "No"
    },
    "reply_unauthorized": {
        "message_id": 2003,
        "from": {"id": 999999999, "is_bot": False, "first_name": "Attacker"},
        "chat": {"id": 999999999, "type": "private"},
        "reply_to_message": {
            "message_id": 1003,
            "chat": {"id": 123456789, "type": "private"}
        },
        "text": "Malicious reply"
    }
}


# Stale callback samples (for expired requests)
STALE_CALLBACKS: Dict[str, Dict[str, Any]] = {
    "expired_allow": {
        "id": "callback-stale-001",
        "from": {"id": 123456789, "is_bot": False, "first_name": "Test User"},
        "message": {
            "message_id": 9001,
            "chat": {"id": 123456789, "type": "private"},
            "text": "*workspace* _session_\n\n*Permission Request* `expired01`\n\nApprove this command?"
        },
        "data": "allow:expired01"
    }
}


# Expected hook outputs for each decision
EXPECTED_HOOK_OUTPUTS: Dict[str, Dict[str, Any]] = {
    "allow": {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow"
            }
        }
    },
    "deny": {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny"
            }
        }
    },
    "stop": {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "interrupt": True
            }
        }
    },
    "whitelist": {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
                "updatedPermissions": {
                    "add": ["Bash(custom_cmd:*)"]
                }
            }
        }
    },
    "reply": {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "reason": "User reply: Please use a different approach"
            }
        }
    }
}


# Expected PreToolUse outputs
EXPECTED_PRETOOL_OUTPUTS: Dict[str, Dict[str, Any]] = {
    "allow": {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow"
        }
    },
    "ask": {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "Test reason"
        }
    }
}


def get_fixture(category: str, name: str) -> Dict[str, Any]:
    """Get a specific fixture by category and name."""
    fixtures = {
        "pretool_payloads": PRETOOL_USE_PAYLOADS,
        "permission_payloads": PERMISSION_REQUEST_PAYLOADS,
        "suggestions": PERMISSION_SUGGESTIONS,
        "callbacks": TELEGRAM_CALLBACKS,
        "replies": TELEGRAM_TEXT_REPLIES,
        "stale_callbacks": STALE_CALLBACKS,
        "hook_outputs": EXPECTED_HOOK_OUTPUTS,
        "pretool_outputs": EXPECTED_PRETOOL_OUTPUTS,
    }

    if category not in fixtures:
        raise ValueError(f"Unknown fixture category: {category}")

    if name not in fixtures[category]:
        raise ValueError(f"Unknown fixture name: {name} in category: {category}")

    return fixtures[category][name].copy()


def list_fixtures() -> Dict[str, List[str]]:
    """List all available fixtures by category."""
    return {
        "pretool_payloads": list(PRETOOL_USE_PAYLOADS.keys()),
        "permission_payloads": list(PERMISSION_REQUEST_PAYLOADS.keys()),
        "suggestions": list(PERMISSION_SUGGESTIONS.keys()),
        "callbacks": list(TELEGRAM_CALLBACKS.keys()),
        "replies": list(TELEGRAM_TEXT_REPLIES.keys()),
        "stale_callbacks": list(STALE_CALLBACKS.keys()),
        "hook_outputs": list(EXPECTED_HOOK_OUTPUTS.keys()),
        "pretool_outputs": list(EXPECTED_PRETOOL_OUTPUTS.keys()),
    }


if __name__ == "__main__":
    # Print all available fixtures
    print("Available Test Fixtures")
    print("=" * 50)

    for category, names in list_fixtures().items():
        print(f"\n{category}:")
        for name in names:
            print(f"  - {name}")
