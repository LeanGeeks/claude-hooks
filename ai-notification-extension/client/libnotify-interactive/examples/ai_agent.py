#!/usr/bin/env python3
"""Example AI agent that requests approval for actions"""

import sys
from notify_interactive import NotificationClient, Action, NotificationOptions


class AIAction:
    """Represents an AI action that needs approval"""

    def __init__(self, description, command, urgency="normal"):
        self.description = description
        self.command = command
        self.urgency = urgency


class AIAgent:
    """AI agent that asks for approval before executing actions"""

    def __init__(self):
        self.client = NotificationClient()
        self.history = []

    def request_approval(self, action: AIAction):
        """Request user approval for an action"""
        print(f"AI wants to: {action.description}")
        print(f"   Command: {action.command}")

        result = self.client.show_and_wait(
            title="AI Action Approval",
            body=f"{action.description}\n\nExecute: {action.command}",
            actions=[
                Action(id="approve", label="Approve"),
                Action(id="deny", label="Deny"),
                Action(id="modify", label="Modify"),
            ],
            options=NotificationOptions(
                urgency=action.urgency,
                code_blocks=[action.command],
            ),
        )

        self.history.append((action, result))

        if result.is_approved:
            print("User approved - executing...")
            return self._execute(action.command)
        elif result.is_denied:
            print("User denied - action cancelled")
            return None
        else:
            print("User wants to modify")
            # Could implement modification dialog here
            return None

    def _execute(self, command):
        """Execute the command (placeholder)"""
        print(f"   Executing: {command}")
        # In real implementation, execute the command
        return f"Executed: {command}"

    def show_history(self):
        """Show approval history"""
        print("\nApproval History:")
        for action, result in self.history:
            status = "APPROVED" if result.is_approved else "DENIED"
            print(f"   [{status}] {action.description}")


# Example usage
if __name__ == "__main__":
    agent = AIAgent()

    # Simulate AI agent actions
    actions = [
        AIAction("Update dependencies", "pip install -r requirements.txt --upgrade"),
        AIAction("Run database migration", "python manage.py migrate", urgency="high"),
        AIAction("Clear cache", "redis-cli FLUSHDB"),
        AIAction("Deploy to production", "kubectl apply -f deployment.yaml", urgency="critical"),
    ]

    for action in actions:
        agent.request_approval(action)
        print()

    agent.show_history()
