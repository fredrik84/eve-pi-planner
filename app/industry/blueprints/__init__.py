"""Blueprint evidence: what the account owns, what it declared, and what its jobs prove.

Split from one 1,517-line module (TODO 34). The public surface is unchanged — every name is
re-exported here, private ones included, because `app/reactions/library.py` imports six of these
and the tests reach for `_batch_key`, `_migrate_location_batches` and `_apply_kind_preference`.

Read `scripts/symbols.sh app/industry/blueprints` (the DIRECTORY) for the map."""

from app.industry.blueprints.esi import (  # noqa: F401
    _PASTE_BATCH_DEFAULT,
    _STACK_CAP,
    _batch_key,
    _blueprint_product_index,
    _copy_rank,
    classify_blueprint,
    ensure_char_blueprints_table,
    fetch_character_blueprints,
    log,
)

from app.industry.blueprints.manual import (  # noqa: F401
    MANUAL_FEATURE_KEY,
    _FORMULA_SUFFIX,
    _apply_kind_preference,
    _formula_stock_buckets,
    _manual_enabled,
    _migrate_location_batches,
    _owned_blueprints,
    _record_unresolved,
    _seen_personally,
    _stock_extra,
    declared_products,
    ensure_manual_blueprints_table,
    ensure_paste_unresolved_table,
    manual_blueprints,
    owned_blueprints,
    paste_unresolved_names,
    stock_formula_prints,
)

from app.industry.blueprints.observed import (  # noqa: F401
    _REACTION_ACTIVITY_ID,
    blueprint_coverage,
    ensure_formula_job_prints_table,
    fetch_formula_job_prints,
    formula_print_floor,
    observed_formula_prints,
    refresh_blueprints,
)

from app.industry.blueprints.paste import (  # noqa: F401
    _STACK_RE,
    _batch_label,
    _default_batch_name,
    _is_number,
    _parse_paste_line,
    _split_location,
    delete_blueprint_batch,
    list_blueprint_batches,
    parse_blueprint_paste,
    replace_blueprint_batch,
)

from app.industry.blueprints.routes import (  # noqa: F401
    BlueprintPaste,
    ManualBlueprintEdit,
    _manual_payload,
    delete_manual_blueprint,
    delete_manual_blueprint_batch,
    edit_manual_blueprint,
    import_manual_blueprint_paste,
    industry_blueprints,
    preview_manual_blueprint_paste,
    read_manual_blueprints,
)

from app.industry.blueprints import (  # noqa: F401  submodules, for tests that patch a module attribute
    esi,
    manual,
    observed,
    paste,
    routes,
)
