-- Visitor Google OAuth state for the tunnel gateway (Render).
-- Run once in the EGDesk Supabase project. Service role access only.

CREATE TABLE IF NOT EXISTS visitor_auth_pending (
  id TEXT PRIMARY KEY,
  tunnel_id TEXT NOT NULL,
  return_to TEXT NOT NULL,
  audience TEXT NOT NULL,
  scopes TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS visitor_auth_pending_expires_idx
  ON visitor_auth_pending (expires_at);

CREATE TABLE IF NOT EXISTS visitor_auth_codes (
  code TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS visitor_auth_codes_expires_idx
  ON visitor_auth_codes (expires_at);

CREATE TABLE IF NOT EXISTS visitor_auth_sessions (
  session_id TEXT PRIMARY KEY,
  tunnel_id TEXT NOT NULL,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  audience TEXT NOT NULL,
  supabase_access_token TEXT NOT NULL,
  supabase_refresh_token TEXT NOT NULL DEFAULT '',
  google_access_token TEXT,
  google_refresh_token TEXT,
  google_expires_at BIGINT,
  scopes TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS visitor_auth_sessions_expires_idx
  ON visitor_auth_sessions (expires_at);

CREATE INDEX IF NOT EXISTS visitor_auth_sessions_tunnel_idx
  ON visitor_auth_sessions (tunnel_id);
