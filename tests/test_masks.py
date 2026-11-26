"""Unit tests for the mask ruleset, the .drx codec and the reference match (no Resolve needed).

    python3 -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
os.environ.setdefault("FLOW_HOME", tempfile.mkdtemp(prefix="flow-test-home-"))

from flow import drx, masks, nodetree, refmatch  # noqa: E402


def frame(sky_rows=0, green=False, w=masks.AW, h=masks.AH, subject=None):
    """Synthetic frame: bright smooth sky on top, mid-grey ground, optional green band and dark subject."""
    img = []
    for y in range(h):
        row = []
        for x in range(w):
            if y < sky_rows:
                c = (0.85, 0.8, 0.75)
            elif green and y > h * 0.75:
                c = (0.25, 0.5, 0.2)
            else:
                c = (0.4, 0.4, 0.4)
            if subject and abs(x / w - subject[0]) < 0.08 and abs(y / h - subject[1]) < 0.12:
                c = (0.05, 0.05, 0.06)
            row.append(c)
        img.append(row)
    return img


class MaskRules(unittest.TestCase):
    def test_sky_detected_with_horizon(self):
        reg = masks.regions(frame(sky_rows=20))
        self.assertGreater(reg["sky"]["area"], 0.3)
        self.assertAlmostEqual(reg["sky"]["horizon"], 20 / masks.AH, delta=1.5 / masks.AH)  # edge row may drop
        d = masks.decide(reg, masks.RULES, sky_target=[0.8, 0.6, 0.4])
        self.assertIn("Sky", d["ops"])
        slope = d["ops"]["Sky"]["cdl"]["slope"]
        self.assertGreater(slope[0], slope[2])              # warmer toward the target sky
        self.assertLess(max(slope), 1.1)                     # and denser, not brighter

    def test_no_sky_no_sky_node(self):
        d = masks.decide(masks.regions(frame()), masks.RULES)
        self.assertNotIn("Sky", d["ops"])
        self.assertIn("no sky", d["why"]["Sky"])

    def test_subject_found_and_vignette_follows(self):
        reg = masks.regions(frame(subject=(0.7, 0.55)))
        self.assertAlmostEqual(reg["subject"]["x"], 0.7, delta=0.08)
        d = masks.decide(reg, masks.RULES)
        self.assertIn("Subject", d["ops"])
        self.assertGreater(d["ops"]["Vignette"]["window"]["cx"], 0.55)   # pulled toward the subject

    def test_subject_hint_wins(self):
        d = masks.decide(masks.regions(frame()), masks.RULES, subject_hint=[0.2, 0.3])
        self.assertEqual(d["ops"]["Subject"]["window"]["cx"], 0.2)

    def test_greens_and_overrides(self):
        reg = masks.regions(frame(green=True))
        self.assertIn("Greens", masks.decide(reg, masks.RULES)["ops"])
        off = masks.rules_with({"greens": False, "sky": {"density": 0.3}})
        self.assertNotIn("Greens", masks.decide(reg, off)["ops"])
        self.assertEqual(off["sky"]["density"], 0.3)

    def test_skin_max_area_guard(self):
        reg = {"luma": 0.5, "sky": {}, "subject": {"conf": 0}, "greens": 0, "skin": 0.4, "highlights": 0}
        d = masks.decide(reg, masks.RULES)
        self.assertNotIn("Skin", d["ops"])
        self.assertIn("warm scene light", d["why"]["Skin"])

    def test_qualifier_luts_are_local(self):
        g = masks.qualifier_transform("greens", masks.RULES["greens"])
        grey = (0.5, 0.5, 0.5)
        self.assertEqual(g(grey), grey)                      # neutral pixels untouched
        out = g((0.2, 0.6, 0.2))
        self.assertLess(abs(out[0] - out[1]), 0.4)           # vivid green pulled toward olive / less saturated
        h = masks.qualifier_transform("highlights", masks.RULES["highlights"])
        self.assertEqual(h((0.3, 0.3, 0.3)), (0.3, 0.3, 0.3))
        self.assertLess(h((1.0, 1.0, 1.0))[2], 1.0)          # top end rolled off


@unittest.skipUnless(drx.zstd_available() and nodetree.TEMPLATE.exists(), "needs zstd and the node template")
class Drx(unittest.TestCase):
    def test_round_trip_is_exact(self):
        xml, grade = drx.read_grade(nodetree.TEMPLATE)
        root = drx.grade_root(grade)
        self.assertEqual(drx.rebuild_grade(grade, root), grade)
        out = Path(tempfile.mkdtemp()) / "t.drx"
        drx.write_grade(xml, grade, out)
        self.assertEqual(drx.read_grade(out)[1], grade)

    def test_mask_tree_layout(self):
        _, grade = drx.read_grade(nodetree.TEMPLATE)
        root = masks.build_tree_root(grade)
        self.assertEqual([drx.node_label(n) for n in drx.nodes(root)], masks.TREE)
        ids = [drx.first(n, 1)[2] for n in drx.nodes(root)]
        self.assertEqual(len(set(ids)), len(ids))
        links = [(drx.first(drx.decode(f[2]), 1)[2], drx.first(drx.decode(f[2]), 3)[2]) for f in drx.get(root, 8)]
        self.assertEqual(links, list(zip(ids, ids[1:])))     # one serial chain, in order
        wins = [lab for n, lab in zip(drx.nodes(root), masks.TREE) if drx.window_params(n)]
        self.assertEqual(wins, ["Sky", "Subject", "Vignette"])

    def test_window_params_written(self):
        ops = {"Subject": {"window": {"cx": 0.75, "cy": 0.5, "ry": 0.2, "rx": 0.2, "invert": False}}}
        out = masks.clip_drx(nodetree.TEMPLATE, ops, Path(tempfile.mkdtemp()) / "c.drx")
        _, grade = drx.read_grade(out)
        node = [n for n in drx.nodes(drx.grade_root(grade)) if drx.node_label(n) == "Subject"][0]
        vals = dict(drx.param_value(p) for p in drx.window_params(node))
        self.assertAlmostEqual(vals[masks.P_MATRIX][2], 270.0, places=1)   # (0.75 - 0.5) * 1080 u
        self.assertEqual(vals[masks.P_INVERT], 0)
        self.assertAlmostEqual(vals[masks.P_RADIUS], 2 * 0.2 * 1080 / (16 / 9), places=0)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RefMatch(unittest.TestCase):
    def _png(self, color, path):
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=96x54",
                        "-frames:v", "1", str(path)], check=True)
        return path

    def test_warm_reference_warms_neutral(self):
        d = Path(tempfile.mkdtemp())
        ref = self._png("0xC08050", d / "ref.png")
        src = self._png("0x808080", d / "src.png")
        m = refmatch.match(ref, [src])
        r, g, b = m["transform"]((0.5, 0.5, 0.5))
        self.assertGreater(r, b)
        self.assertLess(r - b, 0.35)                          # capped, never a flat colour wash


if __name__ == "__main__":
    unittest.main()
