"""Options-pipeline personas. One PERSONA dict per file; the dashboard
reads them via ``shared/personas/_base.discover``. Schema lives in
``shared/personas/_base.py``.

These cover scout (3) + wheel (4) + lesson_analyst across the wheel
state machine that ``pipelines/options/wheel_state.py`` models. Phase
3 follow-on builds will wire them into the actual debate runners.
"""
