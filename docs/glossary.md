# Domain Glossary

Status: initial interview terminology, not a finalized data model

| Term | Meaning |
| --- | --- |
| Repository | A GitHub project selected by user-provided URL and verified as readable by the system; ownership by the user is not required. |
| Repository access | The system's ability to read a repository using its configured identity; this does not imply contribution or merge permissions. |
| Contribution route | The permitted way to publish an issue or propose a repair for the selected repository; exact routing rules are undecided. |
| Configured GitHub account | The account the system is authorized to use for fork creation and issue and PR submission. The specific account is not yet selected. |
| Fork | A repository copy under the configured account used to prepare contributions to the original project; it does not grant authority over the original project. |
| Agent | A Python OpenAI Agents SDK specialist with scoped instructions, tools, and handoff targets. Role boundaries are proposed in the architecture draft. |
| Handoff | An SDK-native transfer of task ownership to another agent. It is not human confirmation, a durable background job, or a grant of permission. |
| Approval interruption | A paused SDK tool call awaiting an application-supplied decision. For this system, the reproduction checkpoint must be backed by the user's actual WebUI confirmation. |
| SDK run state | The resumable SDK execution snapshot associated with a pending run. It is separate from authoritative business records and Docker project data. |
| Finding | A suspected project problem; it is not automatically eligible for publication. |
| Local workspace | A repository checkout on the system's execution host used for analysis and project execution through a separate Docker environment with restricted access. |
| Project environment | A local Docker container environment for installing dependencies, running the project, and executing tests. It does not stay running while waiting for human reproduction; detailed restart, retention, and isolation policies remain to be specified. |
| Reproduction evidence | Evidence demonstrating an observed bug. Human reproduction confirmation is required before issue submission or repair; agent evidence alone is insufficient. Formats and validation rules are undecided. |
| Human reproduction confirmation | The single user's confirmation in the WebUI after personally operating the local project to reproduce a discovered bug. Required before the system proceeds with that finding; required evidence fields are undecided. |
| Analysis report | The record of analysis results, including refactoring, style, and unverified findings excluded from automatic publication. Its delivery format is undecided. |
| Scheduled check | A recurring repository-analysis run triggered by the system's scheduler, once per day by default; execution time, timezone, and other scheduling rules are undecided. |
| WebUI | The single-user web interface for adding repositories, viewing run progress and analysis reports, and recording human reproduction results. Team collaboration is outside version-one scope; authentication and network exposure are undecided. |
| Reproduction reminder | A WebUI-only notice that a finding awaits the user's personal reproduction. Reading the reminder does not confirm the finding. |
| Issue | A GitHub record describing a problem. Publication and duplicate-detection rules are undecided. |
| Repair branch | A separate branch containing proposed changes for a problem. |
| Pull request (PR) | A proposal to merge changes into a target branch. Creating it does not authorize merging it. |
| Project owner | The person responsible for reviewing and merging the PR in the original project, distinct from the system's contribution account. |
| Owner approval | The project owner's affirmative approval of a PR. A review comment alone is not assumed to be approval. |
| Merge | Applying a PR's changes to its target branch. The project owner performs this action; the system does not execute it or enable auto-merge on the owner's behalf. |
| Autonomous action | An action performed without per-action human approval, within configured scope and limits. |
