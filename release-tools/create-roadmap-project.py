"""Create the "DevBrain roadmap" GitHub project from issues labelled `roadmap`.

Requires a gh token with the project scope:  gh auth refresh -s project,read:project
Idempotent: reuses an existing project with the same title and skips issues already on it.
"""
from __future__ import annotations

import json
import subprocess

OWNER, REPO, TITLE = "piotrwozn", "piotrwozn/devbrain", "DevBrain roadmap"
FIELDS = {
    "Priority": ["High", "Medium", "Low", "Research"],
    "Area": ["Memory", "Agents", "UX", "Release", "Tech debt"],
}
AREA_LABELS = {"area: memory": "Memory", "area: agents": "Agents", "area: ux": "UX",
               "area: release": "Release", "tech-debt": "Tech debt"}
PRIORITY_LABELS = {"priority: high": "High", "priority: medium": "Medium", "priority: low": "Low",
                   "research": "Research"}


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


def main() -> None:
    projects = json.loads(gh("project", "list", "--owner", OWNER, "--format", "json", "--limit", "100"))["projects"]
    project = next((p for p in projects if p["title"] == TITLE), None)
    if project is None:
        project = json.loads(gh("project", "create", "--owner", OWNER, "--title", TITLE, "--format", "json"))
        gh("project", "link", str(project["number"]), "--owner", OWNER, "--repo", REPO)
    number = str(project["number"])
    gh("project", "edit", number, "--owner", OWNER, "--readme",
       "Planned work for DevBrain. Each item links to an issue with the problem, proposal, "
       "current state, first slice and acceptance scenario. Milestones group items by release.")

    existing = {f["name"]: f for f in json.loads(gh("project", "field-list", number, "--owner", OWNER,
                                                   "--format", "json", "--limit", "100"))["fields"]}
    for name, options in FIELDS.items():
        if name not in existing:
            gh("project", "field-create", number, "--owner", OWNER, "--name", name,
               "--data-type", "SINGLE_SELECT", "--single-select-options", ",".join(options))
    fields = {f["name"]: f for f in json.loads(gh("project", "field-list", number, "--owner", OWNER,
                                                 "--format", "json", "--limit", "100"))["fields"]}

    items = json.loads(gh("project", "item-list", number, "--owner", OWNER, "--format", "json", "--limit", "500"))
    on_board = {item.get("content", {}).get("url") for item in items["items"]}
    issues = json.loads(gh("issue", "list", "-R", REPO, "--label", "roadmap", "--state", "open",
                           "--limit", "200", "--json", "url,labels,number"))
    for issue in sorted(issues, key=lambda row: row["number"]):
        if issue["url"] in on_board:
            continue
        item = json.loads(gh("project", "item-add", number, "--owner", OWNER, "--url", issue["url"],
                             "--format", "json"))
        labels = {label["name"] for label in issue["labels"]}
        values = {"Area": next((v for k, v in AREA_LABELS.items() if k in labels), None),
                  "Priority": next((v for k, v in PRIORITY_LABELS.items() if k in labels), None)}
        for name, value in values.items():
            if value is None:
                continue
            field = fields[name]
            option = next(o["id"] for o in field["options"] if o["name"] == value)
            gh("project", "item-edit", "--id", item["id"], "--project-id", project["id"],
               "--field-id", field["id"], "--single-select-option-id", option)
        print("added", issue["url"])
    print("Project:", project.get("url", number))


if __name__ == "__main__":
    main()
