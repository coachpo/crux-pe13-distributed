#!/usr/bin/env python3
"""Admit actual dependency waves within the configured single-run Actions slots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/cloud-sharding-profile.json"
WORKFLOW = ROOT / ".github/workflows/build-standard.yml"


def load_policy(path=PROFILE):
    policy = json.loads(Path(path).read_text())
    if (not isinstance(policy, dict) or policy.get("schema_version") != 1
            or not isinstance(policy.get("version"), str) or not policy["version"]
            or any(type(policy.get(key)) is not int or policy[key] <= 0
                   for key in ("wave_capacity", "max_actions", "max_parallel"))):
        raise ValueError("Invalid configured sharding/schedule policy")
    categories = policy.get('category_order')
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(category, str) or not category for category in categories)
            or len(set(categories)) != len(categories)):
        raise ValueError('Invalid configured schedule category enum')
    return policy


def validate_waves(waves, tasks=None, policy=None):
    policy = load_policy() if policy is None else policy
    if (not isinstance(waves, list) or not waves or len(waves) > policy["wave_capacity"]
            or any(not isinstance(wave, list) or not wave for wave in waves)):
        raise ValueError("Actual waves must be nonempty and fit configured wave capacity")
    flattened = [ident for wave in waves for ident in wave]
    if (any(not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ident)
            for ident in flattened) or len(set(flattened)) != len(flattened)):
        raise ValueError("Actual waves must contain unique task IDs")
    if tasks is not None:
        if not isinstance(tasks, dict) or set(tasks) != set(flattened):
            raise ValueError("Actual waves must cover every admitted task exactly once")
        earlier = set()
        for number, wave in enumerate(waves):
            for ident in wave:
                task = tasks[ident]
                if (not isinstance(task, dict) or task.get("id") != ident or task.get("wave") != number
                        or not isinstance(task.get("depends_on"), list)
                        or any(dep not in earlier for dep in task["depends_on"])):
                    raise ValueError("Invalid task binding or uncompleted/same wave dependency: " + ident)
                if task.get('category') not in policy['category_order']:
                    raise ValueError('Task category is outside the configured category enum: ' + ident)
            earlier.update(wave)
    return flattened


def validate_context(context):
    policy = load_policy()
    if context.get("schedule_policy") != policy:
        raise ValueError("Context schedule policy differs from the current configured policy")
    validate_waves(context.get("waves"), context.get("tasks"), policy)
    return policy


def validate_plan(plan):
    policy = load_policy()
    if (plan.get("partition_policy") != policy or plan.get("max_actions") != policy["max_actions"]
            or plan.get("max_parallel") != policy["max_parallel"]):
        raise ValueError("Preparation partition policy differs from the current configured policy")
    rows = plan.get("waves")
    if (not isinstance(rows, list) or any(not isinstance(wave, list) for wave in rows)
            or any(not isinstance(task, dict) for wave in rows for task in wave)):
        raise ValueError("Invalid preparation wave inventory")
    waves = [[task.get("id") for task in wave] for wave in rows]
    tasks = {task.get("id"): task for wave in rows for task in wave}
    validate_waves(waves, tasks, policy)
    return policy, waves


def matrix(context, slot):
    policy = validate_context(context)
    if type(slot) is not int or slot < 0 or slot >= policy["wave_capacity"]:
        raise ValueError("Wave slot is outside configured capacity")
    active = slot < len(context["waves"])
    # A nonempty placeholder keeps Actions matrix parsing valid even though
    # every compute job is skipped for this unused slot.
    return active, {"id": context["waves"][slot] if active else ["unused-slot"]}


def validate_slot_results(context, results):
    policy = validate_context(context)
    expected = {"bootstrap", *("wave" + str(index) for index in range(policy["wave_capacity"]))}
    if set(results) != expected or any(results[name].get("result") != "success" for name in expected):
        raise ValueError("Every declared workflow slot must finish successfully before summary")


def render_workflow(text, policy):
    start = text.index("\n  wave0:\n")
    end = text.index("\n  summary:\n", start)
    names = ["wave" + str(index) for index in range(policy["wave_capacity"])]
    blocks = []
    for index, name in enumerate(names):
        previous = "bootstrap" if index == 0 else names[index - 1]
        needs = "bootstrap" if index == 0 else "[bootstrap, " + previous + "]"
        condition = "!cancelled() && needs.bootstrap.result == 'success'"
        if index:
            condition += " && needs." + previous + ".result == 'success'"
        blocks.append(f"\n  {name}:\n    needs: {needs}\n"
                      f"    if: ${{{{ {condition} }}}}\n"
                      "    uses: ./.github/workflows/compile-standard-wave.yml\n"
                      f"    with:\n      wave: {index}\n")
    text = text[:start] + "".join(blocks) + text[end:]
    all_slots = "[bootstrap, " + ", ".join(names) + "]"
    text = re.sub(r"(\n  summary:\n)    needs: [^\n]+\n(?:    if: [^\n]+\n)?",
                  r"\g<1>    needs: " + all_slots + "\n"
                  "    if: ${{ !cancelled() && !failure() && needs.bootstrap.result == 'success' }}\n", text)
    text = re.sub(r"(\n  report:\n)    needs: [^\n]+", r"\g<1>    needs: [bootstrap, " +
                  ", ".join(names) + ", summary, next]", text)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-workflow", action="store_true")
    args = parser.parse_args()
    if not args.write_workflow:
        parser.error("select --write-workflow to regenerate the declared wave slots")
    text = WORKFLOW.read_text()
    WORKFLOW.write_text(render_workflow(text, load_policy()))


if __name__ == "__main__":
    main()
