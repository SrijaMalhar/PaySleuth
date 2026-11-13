-- Runs once, when the database container is first created (and in CI).
--
-- PaySleuth connects as an ordinary role: a superuser, or a role with
-- BYPASSRLS, would skip row-level security and see every tenant's rows.
CREATE ROLE paysleuth LOGIN PASSWORD 'paysleuth' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE paysleuth OWNER paysleuth;
CREATE DATABASE paysleuth_test OWNER paysleuth;

-- Moments are stored in UTC; Indian time is applied only where a person reads a date.
ALTER DATABASE paysleuth SET timezone = 'UTC';
ALTER DATABASE paysleuth_test SET timezone = 'UTC';
