"""
desc: Local, free, zero-network AI-text-detection pre-check using Binoculars
    (Hans et al., ICML 2024, https://github.com/ahans30/Binoculars) -- a
    zero-shot method that scores text by comparing it under two closely
    related language models (an "observer" and a "performer": base vs.
    instruction-tuned versions of the same model family). No training data,
    no external API calls, nothing leaves this machine.

    The paper's default model pair is tiiuae/falcon-7b + falcon-7b-instruct
    (~14B params combined, ~28GB RAM in bfloat16) -- too heavy to load on
    demand in a laptop-hosted Streamlit app. This module uses the smallest
    practical matched pair from the same family pattern instead
    (HuggingFaceTB/SmolLM2-360M / -Instruct, ~700M params combined), which
    trades some accuracy for keeping BOTH the install (no CUDA-bundled torch
    required -- see requirements-ai-detection.txt) and the runtime footprint
    as light as reasonably possible.

    VALIDATION (measured, not assumed): the paper's published thresholds
    (0.854 "low-fpr" / 0.901 "accuracy") were calibrated on Falcon-7B, an
    ~20x larger pair, and were never re-calibrated for SmolLM2-360M -- so
    this module used to apply them anyway, unvalidated. That's been fixed.
    evaluator/validate_ai_detection.py measured this SmolLM2-360M pair
    (compared against the Qwen2.5-0.5B pair also considered -- Qwen scored
    meaningfully worse, ~88% accuracy / 11% FPR / 12% FNR after its own
    recalibration, and was rejected) against 320 examples of the free,
    public Hello-SimpleAI/HC3 dataset (160 human / 160 ChatGPT answers,
    balanced across HC3's finance/medicine/open_qa/wiki_csai subsets --
    chosen over HC3's larger "reddit_eli5" split for a more semi-formal,
    professional-ish register, closer to cover-letter writing than casual
    forum text). THRESHOLD below (0.7711) was picked on a 160-example
    calibration half of that data and then graded on the other,
    never-seen-by-the-threshold-picker 160-example test half, to keep the
    number honest rather than curve-fit:

        On that 160-example held-out test split (80 human / 80 AI):
        accuracy 98.75%, false-positive rate 0% (0/80 human examples
        wrongly flagged AI), false-negative rate 2.5% (2/80 AI examples
        wrongly passed as human).

    Read those numbers for what they are, not more:
      - n=80 per class is small. An observed 0% FPR is NOT a guarantee of
        0% -- with this sample size the true rate could plausibly be a few
        percent and this test simply didn't happen to sample it.
      - HC3's "AI" text is ChatGPT answering trivia/advice questions, not a
        cover letter. This project's actual cover letters are mostly a
        human-written template with short AI-filled phrases (see
        profile/cover_letter_base.md) -- a different, more mixed-authorship
        case that neither this validation set nor the Binoculars paper
        directly tested. Measured accuracy on whole-document, single-author
        HC3 text may not carry over to that partially-AI-edited case.
      - Only tested against ChatGPT's writing style circa the HC3 dataset's
        collection date, not newer/other frontier models.
      - This is a local, free, English-only heuristic. It is explicitly NOT
        a certified stand-in for GPTZero, and it does not make GPTZero
        redundant -- Noah's actual pass/fail bar stays the manual GPTZero
        spot-check. That bar is itself imperfect: independent audits of
        GPTZero report false-positive rates on human-written text ranging
        from roughly 1% (GPTZero's own claim) to 14-34% depending on study
        and text type, and other AI-writing tools that bundle their own
        detector (e.g. Grammarly, QuillBot) reliably flag their own tools'
        AI-generated/AI-rewritten output as AI too -- i.e. a detector
        correctly flagging genuinely AI-authored text is the expected,
        correct outcome, not evidence the detector is broken. No detector,
        including this one, can guarantee accurate AI-text detection.
      - Re-run/extend this validation with
        `python -m evaluator.validate_ai_detection`; see that file's
        docstring for methodology and why HC3 was chosen over larger
        alternatives (RAID, Kaggle's essay corpus, etc).

    2026-09-21 finding -- this local checker is NOT a reliable GPTZero
    proxy for this project's actual failure mode: Noah got a 100% AI
    result from GPTZero on a generated letter, while this checker scored
    that same shape of letter confidently human (score ~1.01, well above
    THRESHOLD). Root cause turned out to be that profile/cover_letter_base.md's
    "fixed" paragraphs weren't actually verbatim Noah writing -- two of
    them were AI-paraphrased from his resume facts and mislabeled as
    fixed text (see that file's 2026-09-21 correction and the
    "verbatim-not-example" feedback memory). This checker's validation
    set (whole HC3 answers, each wholly one author) never covered that
    failure mode -- "a large AI-paraphrased paragraph dressed up as fixed
    human text" -- so its measured accuracy doesn't transfer to it.
    Recommendation: don't chase a better local detector for this: the
    highest-leverage fix is making the fixed text actually verbatim
    (source it from profile/cover_letter_examples.md directly), which is
    a correctness fix a statistical detector can't substitute for. As a
    same-person calibration point (better than HC3's public data, which
    contains none of Noah's own writing), Noah's real, unedited TikTok
    Campus Intern letter (now profile/cover_letter_examples.md Example 3)
    scores 0.9731 here -- correctly "human," and notably closer to
    THRESHOLD than the generated-letter scores above, which is a useful
    sanity check on how much margin the threshold actually has on known-
    real text.

    Kept intentionally out of the core install: these dependencies live in
    requirements-ai-detection.txt, not requirements.txt, so the core scrape/
    generate/review flow never pulls in torch at all.

    Model loading is lazy (only pays the cost when score_text() is first
    called) and cached as a module-level singleton for the life of the
    process, so a long-running Streamlit session only loads the models once.
    Results are also disk-cached by a hash of the input text under
    .cache/ai_detection/, following the same cache-first pattern already
    used for .cache/ats/ and .cache/coverage/, so re-checking identical text
    never re-runs inference.

args:
    (see score_text() below; no CLI in this module -- see
    tools/check_ai_score.py for a standalone command-line entry point)
output:
    score_text(text) -> dict with "score", "prediction", "model_pair" keys
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "ai_detection"

# Small matched base/instruct pair from the same model family -- keeps the
# Binoculars method (perplexity ratio between the two) meaningful while
# needing roughly 20x fewer combined parameters, and meaningfully smaller
# download, than the paper's Falcon-7B default. Qwen/Qwen2.5-0.5B +
# Qwen2.5-0.5B-Instruct was measured as an alternative (see module
# docstring / evaluator/validate_ai_detection.py) and came out clearly
# worse (~88% accuracy vs ~99% here) -- kept as a documented "already tried,
# rejected" note so nobody re-litigates it without re-running the numbers.
OBSERVER_MODEL = "HuggingFaceTB/SmolLM2-360M"
PERFORMER_MODEL = "HuggingFaceTB/SmolLM2-360M-Instruct"

# Recalibrated for THIS model pair on 320 examples of the HC3 dataset (see
# module docstring for the full validation methodology/numbers) -- NOT the
# Binoculars paper's Falcon-7B thresholds (0.854 / 0.901), which this module
# used to apply unvalidated. Binoculars scores are a ratio where LOWER means
# MORE likely AI-generated; score < THRESHOLD => predicted AI.
THRESHOLD = 0.7710889875888824

_binoculars = None


def _get_binoculars():
    """
    desc: Lazily construct and cache the Binoculars instance for the life of
        this process, using the small SmolLM2-360M pair on CPU in float32
        (bfloat16 is a bfloat16-on-CPU compatibility/perf gamble not worth
        taking for a model this small; float32 keeps it simple and the
        memory cost is trivial at this size).
    args:
        (none)
    output:
        a binoculars.Binoculars instance.
    raises:
        ImportError: if the optional `binoculars`/`torch`/`transformers`
            dependencies aren't installed (they're intentionally NOT in the
            base requirements.txt install path -- see
            requirements-ai-detection.txt).
    """
    global _binoculars
    if _binoculars is None:
        try:
            from binoculars import Binoculars
        except ImportError as e:
            raise ImportError(
                "Local AI-detection pre-check requires the optional "
                "torch/transformers/binoculars dependencies. See "
                "requirements-ai-detection.txt for install instructions."
            ) from e
        _binoculars = Binoculars(
            observer_name_or_path=OBSERVER_MODEL,
            performer_name_or_path=PERFORMER_MODEL,
            use_bfloat16=False,
        )
    return _binoculars


def _cache_path(text: str) -> Path:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def score_text(text: str) -> dict:
    """
    desc: Score `text` with the local Binoculars pre-check, cache-first.
        Loads the (small) model pair on first use only, and reuses a disk
        cache keyed by the exact text so re-checking identical text (e.g. a
        Streamlit rerun re-invoking the same button) never re-runs
        inference.
    args:
        text (str): the cover letter text to score.
    output (dict):
        {"score": float, "prediction": str, "model_pair": "<observer> / <performer>"}
        `prediction` is "Most likely AI-generated" or "Most likely
        human-generated", from comparing `score` against THRESHOLD above
        (recalibrated for this model pair on real validation data -- see
        module docstring) rather than Binoculars' own built-in predict(),
        which applies the paper's Falcon-7B-calibrated threshold regardless
        of which model pair it's constructed with.
        Note: `score` is NOT a "% AI" number like GPTZero reports -- it's
        Binoculars' raw perplexity-ratio score (lower = more likely AI).
        See the module docstring for this tool's measured accuracy/FPR/FNR
        and, just as important, what NOT to conclude from them.
    """
    cache_file = _cache_path(text)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupt/partial cache entry (e.g. process killed mid-write) --
            # treat as a cache miss and recompute, same as the .cache/coverage/
            # and .cache/ats/ readers elsewhere in this codebase, rather than
            # raising and crashing whatever called score_text().
            pass

    bino = _get_binoculars()
    score = float(bino.compute_score(text))
    prediction = (
        "Most likely AI-generated" if score < THRESHOLD
        else "Most likely human-generated"
    )
    result = {
        "score": score,
        "prediction": prediction,
        "model_pair": f"{OBSERVER_MODEL} / {PERFORMER_MODEL}",
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result))
    return result
