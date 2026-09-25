# Ubuntu GUI dependency package

`notification-hub-gui-deps` is a small metapackage. Its `Depends` field lists
the apt packages needed to build the GUI extra and run the GTK client on Ubuntu
24.04 and 26.04. The checked-in `.deb` is bundled into the Python wheel and
installed by `nh-desktop-installer install` after the base tool is installed.
Uninstalling removes the metapackage with `apt-get remove --autoremove`, letting
APT decide which dependencies are no longer needed.

When the dependency list changes, bump `Version` in `DEBIAN/control`, update
`_DEPS_VERSION` in `desktop_integration.py` and the artifact name in
`verify_artifacts.py`, then run `scripts/build-gui-deps-deb.sh`. Commit the
control file and generated `.deb` together.
