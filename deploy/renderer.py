import jinja2


def render(path, cfg):
    """Render a SQL file with the env from config.

    StrictUndefined makes any unknown token (e.g. a typo like
    {{ evn }}) fail loudly instead of rendering empty.
    """
    template = jinja2.Template(path.read_text(), undefined=jinja2.StrictUndefined)
    return template.render(env=cfg["env"])
