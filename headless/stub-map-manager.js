// No-op MapManager replacement for headless runs. getLayerSummaries is called
// by createMapTools at tool-build time to embed the layer list in tool
// descriptions, so we derive it from the DatasetCatalog. All mutating methods
// return a success message — the LLM proceeds as if the map action worked.

export class StubMapManager {
    constructor(catalog) {
        this.catalog = catalog;
    }

    getLayerSummaries() {
        const out = [];
        for (const ds of this.catalog.getAll()) {
            for (const ml of ds.mapLayers || []) {
                out.push({
                    id: `${ds.id}/${ml.assetId}`,
                    displayName: ml.title || ml.assetId,
                    type: /raster|cog/i.test(ml.layerType || '') ? 'raster' : 'vector',
                });
            }
        }
        return out;
    }

    _stub(op, extra = {}) {
        return { success: true, note: `[headless-stub] ${op} not applied (no live map)`, ...extra };
    }

    showLayer(id)     { return this._stub('show_layer',     { layer_id: id }); }
    hideLayer(id)     { return this._stub('hide_layer',     { layer_id: id }); }
    syncCheckbox()    { /* no-op */ }
    setFilter(id, f)  { return this._stub('set_filter',     { layer_id: id, filter: f }); }
    clearFilter(id)   { return this._stub('clear_filter',   { layer_id: id }); }
    resetFilter(id)   { return this._stub('reset_filter',   { layer_id: id }); }
    setStyle(id, s)   { return this._stub('set_style',      { layer_id: id, style: s }); }
    resetStyle(id)    { return this._stub('reset_style',    { layer_id: id }); }
    getMapState()     { return { success: true, note: '[headless-stub] no live map', layers: this.getLayerSummaries() }; }
    flyTo(args)       { return this._stub('fly_to', args); }
    setProjection(a)  { return this._stub('set_projection', a); }
    addHexTileLayer(opts)   { return this._stub('add_hex_tile_layer',    { layer_id: opts && opts.layer_id }); }
    removeHexTileLayer(id)  { return this._stub('remove_hex_tile_layer', { layer_id: id }); }
    setTooltip(id, fields)  { return this._stub('set_tooltip',           { layer_id: id, fields }); }
    resetTooltip(id)        { return this._stub('reset_tooltip',         { layer_id: id }); }
}
