Development
###########

Setup
*****

::

    git clone https://github.com/pyobs/pyobs-web-admin.git
    cd pyobs-web-admin
    uv sync
    uv run python manage.py runserver

See :doc:`installation` for the full development setup, and :doc:`configuration` for
``local_settings.py``.

Tests
*****

::

    uv run python manage.py test modules

Tests live in ``modules/tests.py`` using plain ``unittest.TestCase``, not Django's
``TestCase`` -- this app has no database to wrap in transactions (sessions are signed
cookies, see :doc:`architecture`), so the extra machinery Django's test case provides
doesn't apply here.

Design docs
***********

Non-trivial features get a short design document under ``dev-docs/`` before implementation
(``DEV_ACL_MATRIX.md``, ``DEV_EJABBERD_INTEGRATION.md``, ``DEV_EJABBERD_USER_MANAGEMENT.md``,
``DEV_JOURNALD_LOGS.md``), each following the same shape: Status, Motivation, Current state,
Design, Open questions, Work Plan. ``dev-docs/DEVELOPMENT.md`` is the index pointing at each
one plus a running list of not-yet-designed ideas. These are
implementation journals aimed at whoever picks the feature back up, not end-user
documentation -- the Features pages in this manual (:doc:`features/dashboard` onward) are
the distilled, current-state version of what shipped from each of them.
