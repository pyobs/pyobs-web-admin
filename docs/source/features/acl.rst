ACL matrix and per-module ACL editing
######################################

Background
**********

``pyobs-core`` 2.0 added per-module access control: an ``acl:`` block in a module's own
YAML config, enforced at runtime by that module itself (``Module.execute()``), independent
of any fleet-wide state. That keeps enforcement simple and legible per-module, but it means
that once a fleet has a dozen-plus modules each with their own ``acl:`` block, "who can
reach the telescope, and with what" is scattered across a dozen files with no single place
to read it back. The ACL matrix is a visibility-and-authoring tool over exactly that
problem -- it never touches enforcement, only the config files enforcement reads.

Resolving ``{include}`` and shared fragments
*********************************************

An ``acl:`` block doesn't have to live in a module's own file -- it can arrive via
``{include some.shared.yaml}``, and (like everything else in a pyobs YAML config) can use
YAML anchors/aliases. The matrix reads each module's *effective*, resolved config, not its
literal file content, using the same include-resolution logic ``pyobs-core`` itself uses
(``pyobs.utils.config.pre_process_yaml``), vendored into ``modules/pyobs_config.py`` rather
than re-implemented independently -- two drifting copies of the same include syntax would
be worse than one vendored copy with a note on which ``pyobs-core`` version it was synced
against.

What the matrix shows
**********************

One page, one table: rows are target modules, columns are **callers**. A caller is any
name that appears in *any* module's resolved ``allow``/``deny`` list -- not necessarily a
module this installation itself runs (it could be another app's connecting identity, or a
module on a different host under hub mode). A caller name that happens to match a known
module links to that module's own page.

.. list-table::
   :header-rows: 1

   * - Target's ``acl:``
     - Cell
   * - no ``acl:`` key at all
     - **open** -- every caller, surfaced prominently rather than left blank, since finding
       modules that *should* have a policy and don't is the matrix's main value
   * - ``allow: {caller: "*"}``
     - **all methods**
   * - ``allow: {caller: [m1, m2]}``
     - **m1, m2**
   * - ``allow: {...}``, caller not listed
     - **denied**
   * - ``deny: [caller, ...]``, caller listed
     - **denied**
   * - ``deny: [...]``, caller not listed
     - **all methods**
   * - any of the above with ``mode: log``
     - same value, flagged as **not yet enforced**

An ``allow`` entry may itself be an interface name (e.g. ``ICamera``) rather than a bare
method name -- ``pyobs-core`` expands this at runtime into that interface's full method
list. The matrix does **not** perform this expansion; it shows the entry as-is, badged
``(interface)`` to distinguish it from a literal method name. Reproducing the expansion
statically would mean importing the concrete driver class named in every module's
``class:`` key -- potentially every device package in the fleet -- which reintroduces
exactly the ``pyobs-core`` coupling this app's "no pyobs-core dependency" design avoids for
a purely cosmetic gain.

Editing from the matrix
************************

An edit has to land in the file the rule actually came from:

* If the target's ``acl:`` is **not** behind an ``{include}``, it's edited and saved
  directly -- a structured modal (mode: enforce/log, policy: allow-list/deny-list, add/
  remove caller rows) that writes back via a *text splice*, not a full-file YAML round-trip:
  only the ``acl:`` block's own line range is touched (serialized with ``ruamel.yaml``'s
  round-trip dumper, so it reads like hand-written YAML), and everything else in the file --
  including unrelated ``{include ...}`` lines a generic parser couldn't even load -- is left
  byte-identical. After writing, the block is re-resolved and rolled back to the original
  content if it doesn't match what was requested, as a safety net for the line-detection
  heuristic rather than trying to make that heuristic exhaustively correct.
* If the target's ``acl:`` **is** pulled in from a shared fragment, editing it in place
  would silently change every other module that includes the same fragment. The matrix
  instead shows a badge ("this rule comes from ``acl.shared.yaml``, included by 4 modules")
  linking to that fragment's own editor -- it does not offer a one-click "detach into a
  module-local override," by design; the operator decides by hand.

**Two editing surfaces, one backend.** Besides the matrix's own per-row modal, each
module's detail page has its own *ACL* tab -- a full editor listing every other managed
module as a click-to-allow/deny row, plus a free-text row for callers that aren't a known
module. Both surfaces call the same underlying save path and storage format; they're kept
as two independent UIs (not one shared component) because the two pages have different
host-context models -- the matrix aggregates every host on one page, while a module's own
detail page always shows one host at a time, matching its other tabs.

Hub mode
********

Unlike most of this app's hub-mode views (dashboard/config/logs), which show one host at a
time via the session's active-host switcher, the matrix's whole point is fleet-wide
visibility -- it always queries **every** configured host and merges the results into one
table, tagging each row with its host and recomputing cells against the union of all hosts'
callers. An unreachable host is shown as a warning banner, with the rest of the matrix still
usable, rather than failing the whole page. See :doc:`../hub` for the underlying proxying
mechanism this reuses.
