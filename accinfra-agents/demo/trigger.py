"""
AccInfra Demo — Manual Agent Trigger
─────────────────────────────────────
Run any agent on-demand instead of waiting for the cron schedule.
Perfect for live demos: fire each stage exactly when you narrate it.

Usage (from the accinfra-agents directory, on the EC2 instance):
    python demo/trigger.py issue-watcher
    python demo/trigger.py solution-architect
    python demo/trigger.py review-handler
    python demo/trigger.py deploy-closer
    python demo/trigger.py all          # runs all 4 in sequence

This is the same code the crons run — just invoked manually so you
control the timing in front of an audience.
"""

import sys
import os
import time
import importlib.util

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

AGENTS = {
    "issue-watcher": "agents/issue-watcher/run.py",
    "solution-architect": "agents/solution-architect/run.py",
    "review-handler": "agents/review-handler/run.py",
    "deploy-closer": "agents/deploy-closer/run.py",
}

# Human-friendly labels for demo narration
LABELS = {
    "issue-watcher": "Agent 1: Issue Watcher — detecting new accinfra: issues",
    "solution-architect": "Agent 2: Solution Architect — infra check + Terraform + PR",
    "review-handler": "Agent 3: Review Handler — applying reviewer feedback",
    "deploy-closer": "Agent 4: Deploy Closer — closing issue after deploy",
}


def run_agent(agent_key: str):
    """Dynamically import and run a single agent's run() function."""
    rel_path = AGENTS[agent_key]
    abs_path = os.path.join(PROJECT_ROOT, rel_path)

    print("\n" + "=" * 70)
    print(f"  ▶  {LABELS[agent_key]}")
    print("=" * 70)

    start = time.time()

    # Load the agent module dynamically
    spec = importlib.util.spec_from_file_location(f"agent_{agent_key}", abs_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Every agent exposes a run() entrypoint
    module.run()

    elapsed = round(time.time() - start, 1)
    print(f"\n  ✓  Done in {elapsed}s")
    print("=" * 70 + "\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python demo/trigger.py <agent-name|all>")
        print(f"Agents: {', '.join(AGENTS.keys())}, all")
        sys.exit(1)

    target = sys.argv[1].strip().lower()

    if target == "all":
        print("\n🚀 Running ALL agents in sequence (full pipeline pass)...\n")
        for agent_key in AGENTS:
            run_agent(agent_key)
            time.sleep(2)  # small pause between agents for readability
        print("\n✅ Full pipeline pass complete.\n")
    elif target in AGENTS:
        run_agent(target)
    else:
        print(f"Unknown agent: {target}")
        print(f"Valid: {', '.join(AGENTS.keys())}, all")
        sys.exit(1)


if __name__ == "__main__":
    main()
