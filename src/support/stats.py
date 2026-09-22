from pprint import pprint
import json
import os
import threading

_ENTITIES = ["suites", "cases", "runs", "milestones", "shared_steps", "configurations"]


class Stats:
    def __init__(self, source: str):
        self.projects = {}
        self.source = source
        self.attachments = {
            self.source: 0,
            "qase": 0
        }
        self.users = {
            self.source: 0,
            "qase": 0
        }
        self.custom_fields = {
            self.source: 0,
            "qase": 0
        }
        # Per-project list of {"level", "entity", "message"}: everything the
        # run skipped, degraded, or failed, so the end-of-run report can say
        # WHY counts differ instead of just showing them (counts alone can
        # overstate success). Populated automatically from Logger warnings.
        self.issues = {}
        self._lock = threading.Lock()

    def add_project(self, code: str, title: str):
        self.projects[code] = {
            "title": title,
            self.source: {e: 0 for e in _ENTITIES},
            "qase": {e: 0 for e in _ENTITIES},
        }

    def add_user(self, type: str, count: int = 1):
        self.users[type] += count

    def add_attachment(self, type: str, count: int = 1):
        # Called from attachment coroutines running concurrently per case/execution
        with self._lock:
            self.attachments[type] += count

    def add_custom_field(self, type: str, count: int = 1):
        self.custom_fields[type] += count

    def add_entity_count(self, code: str, entity: str, type: str, count: int = 1):
        with self._lock:
            project = self.projects.get(code)
            if not project:
                return
            project.setdefault(type, {}).setdefault(entity, 0)
            project[type][entity] += count

    def add_issue(self, code: str, entity: str, message: str, level: str = "warn"):
        """Record a skipped/degraded/failed item for the end-of-run report."""
        with self._lock:
            self.issues.setdefault(code or "-", []).append(
                {"level": level, "entity": entity, "message": str(message).strip()}
            )

    def print(self):
        print("------ Stats ------")
        print()
        state = {k: v for k, v in vars(self).items() if not k.startswith("_") and k != "issues"}
        pprint(state, depth=4, sort_dicts=False)

    def print_issues(self, max_lines: int = 60):
        """End-of-run report: everything that was skipped or degraded, with reasons."""
        n_total = sum(len(v) for v in self.issues.values())
        if not n_total:
            print("\n------ Migration report: no skipped or degraded items ------")
            return
        print(f"\n------ Migration report: {n_total} skipped/degraded item(s) ------")
        shown = 0
        for code in sorted(self.issues):
            items = self.issues[code]
            print(f"\n  [{code}] · {len(items)} item(s)")
            for issue in items:
                if shown >= max_lines:
                    print(f"\n  … +{n_total - shown} more, see the stats JSON and the log file")
                    return
                icon = {"error": "✗", "info": "·"}.get(issue["level"], "!")
                print(f"    {icon} [{issue['entity']}] {issue['message']}")
                shown += 1

    def save(self, prefix: str = ''):
        filename = f'{prefix}_stats.json'
        stats_dir = './stats'
        if not os.path.exists(stats_dir):
            os.makedirs(stats_dir)
        stats_file = os.path.join(stats_dir, f'{filename}')
        state = {k: v for k, v in vars(self).items() if not k.startswith("_")}
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=4)

    def save_xlsx(self, prefix: str = ''):
        try:
            # Imported here, not at module scope: a missing pandas/openpyxl must
            # cost the XLSX only, not take the whole run down on import.
            import pandas as pd

            filename = f'{prefix}_stats.xlsx'
            stats_dir = './stats'
            if not os.path.exists(stats_dir):
                os.makedirs(stats_dir)
            stats_file = os.path.join(stats_dir, f'{filename}')

            data_for_comparison = {
                'Project Code': [],
                'Title': [],
                'Entity': [],
                'Qase': [],
                self.source.capitalize(): []
            }

            for code, project in self.projects.items():
                for entity in _ENTITIES:
                    # Append project code and title for each entity to keep list lengths equal
                    data_for_comparison['Project Code'].append(code)
                    data_for_comparison['Title'].append(project['title'])
                    data_for_comparison['Entity'].append(entity)
                    data_for_comparison['Qase'].append(project['qase'].get(entity, 0))
                    data_for_comparison[self.source.capitalize()].append(
                        project[self.source].get(entity, 0)
                    )

            df = pd.DataFrame(data_for_comparison)

            issues_rows = {'Project Code': [], 'Level': [], 'Entity': [], 'Message': []}
            for code in sorted(self.issues):
                for issue in self.issues[code]:
                    issues_rows['Project Code'].append(code)
                    issues_rows['Level'].append(issue['level'])
                    issues_rows['Entity'].append(issue['entity'])
                    issues_rows['Message'].append(issue['message'])

            with pd.ExcelWriter(stats_file, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Comparison')
                pd.DataFrame(issues_rows).to_excel(
                    writer, index=False, sheet_name='Migration report'
                )
        except Exception as e:
            # Never fail the run over the XLSX, and never fail silently either:
            # without this, a missing pandas/openpyxl just means no file appears.
            print(f"\t\033[33m!\033[0m [warn] Stats XLSX not written: {e!r} (JSON stats are saved)")
