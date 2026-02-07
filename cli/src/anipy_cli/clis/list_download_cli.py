import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from anipy_api.anime import Anime
from anipy_api.provider import LanguageTypeEnum, list_providers

from anipy_cli.clis.base_cli import CliBase
from anipy_cli.colors import colors, cprint
from anipy_cli.config import Config
from anipy_cli.download_component import DownloadComponent
from anipy_cli.prompts import (
    lang_prompt,
    pick_episode_range_prompt,
    search_show_multi_prompt,
)
from anipy_cli.util import error

if TYPE_CHECKING:
    from anipy_api.provider import Episode

    from anipy_cli.arg_parser import CliArgs


class ListDownloadCli(CliBase):
    def __init__(self, options: "CliArgs"):
        super().__init__(options)

        self.list_file: Path = options.download_list
        self.state_file: Path = self.list_file.with_suffix(".json")
        self.dl_path = Config().download_folder_path
        if options.location:
            self.dl_path = options.location

        self.anime_names: List[str] = []
        self.state: Dict[str, Any] = {"entries": []}
        self.picked: List[Tuple[Anime, LanguageTypeEnum, List["Episode"]]] = []

    def print_header(self):
        cprint(colors.GREEN, "***List Download Mode***")

        if not self.list_file.is_file():
            error(f"file not found: {self.list_file}", fatal=True)

        self.anime_names = [
            line.strip()
            for line in self.list_file.read_text().splitlines()
            if line.strip()
        ]

        if not self.anime_names:
            error("the list file is empty", fatal=True)

        if self.state_file.is_file():
            try:
                self.state = json.loads(self.state_file.read_text())
                completed = sum(
                    1
                    for e in self.state["entries"]
                    if e["status"] == "completed"
                )
                total = len(self.state["entries"])
                cprint(
                    colors.GREEN,
                    "Resuming from: ",
                    colors.END,
                    f"{self.state_file.name} ({completed}/{total} completed)",
                )
            except (json.JSONDecodeError, KeyError):
                self.state = {"entries": []}

        cprint(
            colors.GREEN,
            "Reading list from: ",
            colors.END,
            str(self.list_file),
        )
        cprint(
            colors.GREEN,
            "Downloads are stored in: ",
            colors.END,
            str(self.dl_path),
        )

    def _find_state_entry(self, query: str) -> Optional[Dict[str, Any]]:
        for entry in self.state["entries"]:
            if entry["query"] == query:
                return entry
        return None

    def _save_state(self):
        self.state_file.write_text(json.dumps(self.state, indent=2))

    def _resolve_anime(self, provider_name: str, identifier: str, name: str, languages: List[str]) -> Anime:
        for p_cls in list_providers():
            if p_cls.NAME == provider_name:
                config = Config()
                url_override = config.provider_urls.get(p_cls.NAME, None)
                provider = p_cls(url_override)
                return Anime(
                    provider=provider,
                    name=name,
                    identifier=identifier,
                    languages={LanguageTypeEnum[l.upper()] for l in languages},
                )
        error(f"provider '{provider_name}' not found", fatal=True)

    def take_input(self):
        total = len(self.anime_names)

        for idx, query in enumerate(self.anime_names, 1):
            existing = self._find_state_entry(query)

            if existing is not None:
                if existing["status"] == "completed":
                    cprint(
                        colors.GREEN,
                        f"\n--- [{idx}/{total}] ",
                        colors.END,
                        f'"{query}" ',
                        colors.GREEN,
                        "(already completed) ---",
                    )
                    continue

                # Entry exists and is pending — already resolved, skip prompts
                cprint(
                    colors.GREEN,
                    f"\n--- [{idx}/{total}] ",
                    colors.END,
                    f'"{query}" ',
                    colors.GREEN,
                    "(resuming from saved state) ---",
                )
                continue

            # New entry — interactive prompts
            cprint(
                colors.GREEN,
                f"\n--- [{idx}/{total}] Searching for ",
                colors.BLUE,
                f'"{query}"',
                colors.GREEN,
                " ---",
            )

            selected_anime = search_show_multi_prompt("download", query)

            if not selected_anime:
                cprint(colors.RED, f"Skipping '{query}' (no selection made)")
                continue

            entry: Dict[str, Any] = {
                "query": query,
                "anime": [],
                "status": "pending",
            }

            for anime in selected_anime:
                lang = lang_prompt(anime)
                episodes = pick_episode_range_prompt(anime, lang)

                if not episodes:
                    cprint(
                        colors.RED,
                        f"Skipping '{anime.name}' (no episodes selected)",
                    )
                    continue

                entry["anime"].append(
                    {
                        "provider": anime.provider.NAME,
                        "identifier": anime.identifier,
                        "name": anime.name,
                        "languages": [l.value for l in anime.languages],
                        "lang": lang.value,
                        "episodes": [
                            int(e) if isinstance(e, int) or (isinstance(e, float) and e.is_integer()) else float(e)
                            for e in episodes
                        ],
                    }
                )

            if entry["anime"]:
                self.state["entries"].append(entry)

        # Save state after all interactive input
        self._save_state()
        cprint(
            colors.GREEN,
            "\nSaved selections to: ",
            colors.END,
            str(self.state_file),
        )

        # Build the picked list from all pending entries
        for entry in self.state["entries"]:
            if entry["status"] == "completed":
                continue

            for anime_data in entry["anime"]:
                anime = self._resolve_anime(
                    anime_data["provider"],
                    anime_data["identifier"],
                    anime_data["name"],
                    anime_data["languages"],
                )
                lang = LanguageTypeEnum[anime_data["lang"].upper()]
                episodes = [
                    int(e) if isinstance(e, (int, float)) and float(e).is_integer() else float(e)
                    for e in anime_data["episodes"]
                ]
                self.picked.append((anime, lang, episodes))

        if not self.picked:
            cprint(colors.GREEN, "\nAll anime already downloaded!")
            return False

    def process(self):
        if not self.picked:
            return

        # Track which entry each (anime, lang, episodes) tuple belongs to
        # so we can mark entries completed incrementally
        entry_anime_map: Dict[int, List[int]] = {}
        pick_idx = 0
        for entry_idx, entry in enumerate(self.state["entries"]):
            if entry["status"] == "completed":
                continue
            for _ in entry["anime"]:
                entry_anime_map.setdefault(entry_idx, []).append(pick_idx)
                pick_idx += 1

        errors = DownloadComponent(
            self.options, self.dl_path, "download"
        ).download_anime(
            self.picked,
            after_success_ep=lambda anime, ep, lang: None,
            only_skip_ep_on_err=True,
            sub_only=self.options.subtitles,
        )

        # After download completes, mark all non-failed entries as completed
        failed_anime_names = {a.name for a, _ in errors}

        for entry_idx, pick_indices in entry_anime_map.items():
            entry = self.state["entries"][entry_idx]
            all_ok = True
            for pi in pick_indices:
                anime, lang, eps = self.picked[pi]
                if anime.name in failed_anime_names:
                    all_ok = False
                    break
            if all_ok:
                entry["status"] = "completed"

        self._save_state()

        DownloadComponent.serve_download_errors(errors, only_skip_ep_on_err=True)

    def show(self):
        pass

    def post(self):
        pass
