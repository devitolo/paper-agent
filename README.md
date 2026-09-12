# Project Paper

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Project Paper is a local-first system for discovering, curating, reviewing, and learning from research papers based on a user's evolving interests and feedback.

> **First supported release:** the installer-fronted Docker Compose path and manual in-product Scout have passed exact-image acceptance on Ubuntu x86-64. The same v0.1.3 image also passed macOS Apple Silicon acceptance. See the [package usage guide](docs/m1-package-usage.md), [acceptance report](docs/m1-qa-report.md), and [productization plan](docs/productization-plan.md).

The repository currently contains a Python MVP called `paper_agents`. The V2 backend separates Scout, Curator, and Feedback responsibilities: Scout retrieves candidate pools, Curator scores and recommends papers, and the Feedback Agent stores pasted ChatGPT discussion summaries in immutable SQLite history with deterministic v1 parsing.

## First Supported Path

The current first-user path is:

1. Use a fresh clone on Ubuntu x86-64 with Docker Engine and Compose v2, or macOS Apple Silicon with Docker Desktop.
2. Run the installer; its defaults select the public Project Paper image and pinned Ollama image.
3. Open the loopback web UI.
4. Add an enabled arXiv topic in **Topics**.
5. Return to **Review Queue** and choose **Run Scout**.
6. Review returned papers and save feedback. For optional external discussion, copy the discussion prompt and paste the final feedback blob back into Project Paper.

From a clean checkout:

```bash
cd /absolute/path/to/paper-agent
bash scripts/install_project_paper.sh
```

The installer creates `.env` if missing, selects the immutable `v0.1.3` image digest, prepares the default local Qwen model in Docker, starts the app on a loopback URL, and prints runtime/log/retry commands. Docker selects the matching architecture image automatically. The packaged path requires Docker with Compose; it does not require host Python, host Ollama, cron, systemd, OpenAI, Gemini, OpenAlex, or Semantic Scholar credentials for the default arXiv flow.

Gemini profile synthesis is disabled in this package. Feedback blobs are still saved, parsed when possible, and available to deterministic Scout guidance, but direct profile evolution is not promised without the later optional Gemini packaging work.

The packaged runtime stores user state in Compose volumes: `paper-data` for SQLite/artifacts/profile, `paper-config` for topics, and `ollama-data` for the model cache. Re-run the installer to repair/retry without deleting volumes. Do not use `db reset`, remove Compose volumes, or change the recorded project name as a normal recovery or upgrade step.

Use the [packaged backup/restore guide](docs/package-backup.md) to preserve user state and restore into empty volumes with the same image. Upgrade/rollback remains deferred. Support is through [GitHub Issues](https://github.com/devitolo/paper-agent/issues) for the current major version only.

## Requirements and limits

Use Ubuntu x86-64 with Docker Engine and Compose v2, or macOS Apple Silicon with Docker Desktop. Git, network access for image/model downloads and arXiv, and enough Docker storage are required. The installer checks provisional 4 GiB RAM and 6 GiB free-space thresholds; these are preflight guards, not qualified minimum hardware specifications.

Discovery is manual in the package. arXiv can rate-limit requests; local inference can be slow on CPU. Runtime status explains failures and permits retry. Automated scheduling, packaged optional providers, upgrades/rollback, multi-user support, deep reading, Go migration and advanced Topic Agent work remain outside this release scope. Ranking quality is still being validated.

External discussion is a manual handoff. Review the copied content before sending it to a cloud service; local feedback storage does not require a ChatGPT integration.

## Documentation

- [Install, review, diagnostics and recovery](docs/m1-package-usage.md)
- [Backup and restore](docs/package-backup.md)
- [Release acceptance evidence](docs/m1-qa-report.md)
- [Productization scope and remaining gates](docs/productization-plan.md)
- [Architecture](docs/architecture.md)
- [Native Mini and historical prototype reference](docs/native-operations-history.md)

The historical reference is for maintaining the existing Mini. Its old Compose commands and provider requirements are not customer setup instructions.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
