import unittest

from GalTransl.server import RuntimeRegistry


class RuntimeFilenameMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RuntimeRegistry()
        self.project_dir = r"E:\\tmp\\galtransl_project"

    def test_success_event_uses_display_name_for_split_cache_filename(self) -> None:
        self.registry.update_status(
            self.project_dir,
            file_totals={"chapter/scene.json": 100},
            cache_file_display_map={
                "chapter-}scene.json_1.json": "chapter/scene.json",
                "chapter-}scene.json_2.json": "chapter/scene.json",
            },
        )

        self.registry.append_success(
            self.project_dir,
            filename="chapter-}scene.json_1",
            index=12,
            speaker="A",
            source_preview="src",
            translation_preview="dst",
            trans_by="model",
        )

        snapshot = self.registry.get_runtime_snapshot(self.project_dir)
        self.assertEqual(snapshot["recent_successes"][0]["filename"], "chapter/scene.json")

    def test_error_event_strips_split_suffix_with_file_totals_fallback(self) -> None:
        self.registry.update_status(
            self.project_dir,
            file_totals={"chapter/scene.json": 100},
            cache_file_display_map={},
        )

        self.registry.append_error(
            self.project_dir,
            kind="parse",
            message="bad output",
            filename="chapter/scene.json_2",
            index_range="1~3",
            level="warning",
        )

        snapshot = self.registry.get_runtime_snapshot(self.project_dir)
        self.assertEqual(snapshot["recent_errors"][0]["filename"], "chapter/scene.json")


if __name__ == "__main__":
    unittest.main()
