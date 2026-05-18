# Fiji/ImageJ Update Sites on GitHub Pages

This repository builds one GitHub Pages deployment containing multiple Fiji/ImageJ
update sites:

- one directory per plugin, for selective installation
- `all/`, containing every plugin JAR for one-shot installation

Each update site is self-contained and has its own `db.xml.gz`.

## Configure plugins

Edit `plugins.json` and add one entry per public plugin source repository.
Use `plugins.example.json` as the template.

Required fields:

- `name`: human-readable plugin name
- `repo`: public Git URL
- `ref`: branch or tag to build
- `site_dir`: URL directory for the plugin update site
- `build`: shell command, or an array of shell commands
- `build_dir`: directory where the build command runs, default `.`
- `jar_glob`: glob for the built JARs, usually `target/*.jar`

Optional fields:

- `copy_as`: rename the selected JAR in the update site; useful to avoid
  duplicate filenames in `all/plugins/`
- `exclude_jars`: filename patterns to ignore
- `dependencies`: files from `all/` to also copy into this plugin's individual
  update site, for example `plugins/ROI_Explorer_Fiji.jar`

Private source repositories require a fine-grained GitHub token with read access
stored as the `SOURCE_REPO_TOKEN` repository secret.

## Publish

Enable GitHub Pages with **Source: GitHub Actions** in repository settings.
Then run the `Publish Fiji Update Sites` workflow.

The published URLs will be:

```text
https://<owner>.github.io/<repo>/<site_dir>/
https://<owner>.github.io/<repo>/all/
```

Add one of those URLs in Fiji via:

```text
Help > Update > Manage update sites > Add
```

## Local test

```bash
python3 scripts/build_update_sites.py --config plugins.json --work-dir _work --dist public
```

The generated `public/` directory is what GitHub Pages deploys.
