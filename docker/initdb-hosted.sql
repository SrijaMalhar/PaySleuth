-- Role setup for a hosted PostgreSQL (Neon, Supabase, RDS, Railway).
--
-- The local equivalent is initdb.sql, which the container runs for you. On a
-- hosted service you cannot run initdb, and the role the provider hands you is
-- the problem this file exists to solve.
--
-- A managed provider gives you an owner role that is a member of its own
-- administrative role -- neon_superuser on Neon, supabase_admin on Supabase --
-- and those carry BYPASSRLS. Connect as one and every policy in schema.sql is
-- skipped silently: no error, no warning, just one tenant able to read
-- another's rows. check_role() in db.py refuses to start as such a role, which
-- is why a deploy fails here rather than later and quietly.
--
-- Create the role with SQL, not with the provider's dashboard. A role added
-- through the dashboard is granted that same administrative role, so it
-- inherits BYPASSRLS and fails check_role() exactly like the owner does.
--
-- Run this as the owner role the provider gave you, then connect the
-- application as `paysleuth`.

CREATE ROLE paysleuth LOGIN PASSWORD 'change-me'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Substitute the provider's database name: neondb on Neon, postgres on most
-- others. PostgreSQL 15 and later revoke CREATE on schema public from PUBLIC,
-- so the grant is required rather than redundant.
GRANT CONNECT, TEMPORARY ON DATABASE neondb TO paysleuth;
GRANT CREATE, USAGE ON SCHEMA public TO paysleuth;

-- Moments are stored in UTC; Indian time is applied only where a person reads
-- a date. Matches what initdb.sql sets locally.
ALTER DATABASE neondb SET timezone = 'UTC';

-- The application creates the tables, so `paysleuth` owns them. That is safe
-- here and would not be without FORCE: schema.sql declares FORCE ROW LEVEL
-- SECURITY, which subjects the owner to the policies too. Without FORCE, a
-- table's owner bypasses RLS no matter what its role attributes say.
