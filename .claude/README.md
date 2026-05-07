# Claude Code Command Allow/Deny Lists

This directory contains configuration for controlling which commands Claude Code can run without asking for approval.

## Files

- `settings.json` - Project-level settings (can be committed to git)
- `settings.local.json` - Local overrides (gitignored, for workspace-specific settings)

## Reusing in Other Workspaces

To use this configuration in another project:

```bash
# Copy the settings file
cp /path/to/ai-playground/.claude/settings.json /path/to/other-project/.claude/

# Or create a symlink (shared config)
ln -s /path/to/ai-playground/.claude/settings.json /path/to/other-project/.claude/settings.json
```

## Settings Hierarchy

Claude Code loads settings in this order (later settings override earlier ones):

1. User settings: `~/.claude/settings.json`
2. Project settings: `<project>/.claude/settings.json`
3. Local settings: `<project>/.claude/settings.local.json`

## Pattern Syntax

### Bash Commands
- `Bash` - Allow all bash commands
- `Bash(git:*)` - Allow any git command
- `Bash(git status)` - Allow only exact command
- `Bash(npm run:*)` - Allow any npm run command

### File Tools
- `Read` - Allow reading any file
- `Read(*.ts)` - Allow reading .ts files
- `Read(src/**)` - Allow reading files in src/

### Other Tools
- `Edit` - Allow all edits
- `Grep` - Allow all searches
- `Task` - Allow agent tasks

## Categories in This Configuration

### Allowed (Safe Commands)
- File operations: Read, Write, Edit, Glob, Grep
- Info commands: ls, pwd, cat, head, tail, find, grep, etc.
- Git commands: all git operations
- Language runtimes: node, python, go, rust, java, etc.
- Package managers: npm, yarn, pnpm, pip, cargo, etc.
- Build tools: make, cmake, mvn, gradle, etc.
- Dev tools: docker, kubectl, terraform, etc.
- Test runners: pytest, jest, vitest, etc.

### Disallowed (Dangerous Commands)
- File deletion: rm, rmdir, shred
- Permission changes: chmod, chown, chattr
- Privilege escalation: sudo, su, doas, pkexec
- System control: systemctl, service, reboot, shutdown
- Process killing: kill, killall, pkill
- Disk operations: mount, fdisk, mkfs, dd
- Firewall: iptables, nftables, ufw
- Environment modifiers: nvm, rbenv, pyenv, asdf

## Customizing

Edit `settings.json` to add or remove patterns:

```json
{
  "allowedTools": [
    "Bash(your-command:*)"
  ],
  "disallowedTools": [
    "Bash(dangerous:*)"
  ]
}
```

## Testing

Start Claude Code in this directory and try commands. They should run without prompts:

```bash
cd /path/to/ai-playground
claude
# Try: "List files in this directory"
```

## Notes

- `disallowedTools` takes precedence over `allowedTools`
- Patterns are matched using glob-style syntax
- The `:*` suffix means "allow anything starting with this prefix"

## Hook Lifecycle

Claude Code uses a hook system for command validation and permission handling. Understanding the lifecycle is crucial for debugging and customization.

### Hook Events

1. **PreToolUse**: Triggered before a tool is executed
   - Fast classifier that validates commands
   - Can return `allow`, `ask`, or `deny`
   - For Bash commands: parses compound commands and validates each sub-command

2. **PermissionRequest**: Triggered when PreToolUse returns `ask`
   - Handles user interaction for non-auto-approved commands
   - Can integrate with external approval systems (e.g., Telegram)
   - Returns final decision based on user input

3. **Notification**: Triggered for idle notification events
   - `idle_prompt`: When Claude is waiting for input
   - Permission prompts are handled by `PermissionRequest` and forwarded to Telegram

### Decision Flow

```
Tool Call (e.g., Bash command)
         |
         v
    PreToolUse Hook
         |
    +----+----+
    |         |
    v         v
 allowed?   ask/deny
    |         |
    v         v
 Execute   PermissionRequest Hook
    |         |
    |    +----+----+
    |    |         |
    |    v         v
    |  approve   deny
    |    |         |
    |    v         v
    | Execute   Block
```

### Why `ask` Instead of `defer`

Previously, the PreToolUse hook would "defer" for unknown commands, which meant it exited silently and let Claude's default permission system handle it. This had several issues:

1. **Non-deterministic**: The PermissionRequest hook might not be called consistently
2. **No correlation**: Hard to track which permission prompt relates to which command
3. **Limited control**: No way to route specific commands to specific handlers

By explicitly returning `ask`, we ensure:

1. **Deterministic flow**: PermissionRequest hook is always triggered
2. **Better logging**: Session ID and command metadata are preserved
3. **Flexible routing**: Future handlers can make routing decisions based on command type

### Debugging

Enable debug logging:

```bash
export CLAUDE_HOOK_DEBUG=1
```

View logs:

```bash
tail -f ~/.claude/bash_hook_debug.log
tail -f ~/.claude/bash_manual_confirm.log
```
