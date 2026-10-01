/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_WS_BASE?: string;
  readonly VITE_BASEMAP?: string;
  readonly VITE_LOCAL_TILE_URL?: string;
  readonly VITE_LOCAL_TILE_MAXZOOM?: string;
  readonly VITE_SATELLITE_TILE_URL?: string;
  readonly VITE_STREET_TILE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
