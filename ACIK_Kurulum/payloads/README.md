# Private deployment payloads are not included

This public source delivery intentionally excludes installer executables,
connection profiles, certificate material, boot assets, and any configuration
that could contain credentials or organization-specific settings.

For an internal deployment build, obtain approved payloads through the private
release process, keep them outside this public directory, generate the payload
integrity manifest there, and never commit or publish them with the source.
