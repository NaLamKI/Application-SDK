"""Manifest rules 1-6 of concepts/app-store.md section 3, one assertion per rule."""
from __future__ import annotations

import copy

import pytest

from appext.manifest import Manifest, ManifestError, load_manifest, loads_manifest, parse_manifest

from .conftest import MANIFEST

BASE = {
    "extension": {
        "id": "demo", "name": "Demo", "version": "1.2.0", "entry": "/", "icon": "icon.svg",
    },
    "consent": {"scopes": ["ext-data-read"]},
    "services": [
        {"name": "projects", "audience": "projects-api", "scopes": ["svc-projects-read"], "mode": "user"},
        {"name": "export", "audience": "export-api", "scopes": ["svc-export-write"], "mode": "service"},
    ],
}


def variant(**changes):
    """BASE with dotted-path changes; a value of `DELETE` removes the key."""
    data = copy.deepcopy(BASE)
    for path, value in changes.items():
        node = data
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
        last = parts[-1]
        if value is DELETE:
            del node[int(last) if isinstance(node, list) else last]
        elif isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return data


DELETE = object()


def error_paths(data) -> list[str]:
    with pytest.raises(ManifestError) as err:
        parse_manifest(data)
    return err.value.paths


def test_a_valid_manifest_is_read():
    m = loads_manifest(MANIFEST)
    assert isinstance(m, Manifest)
    assert (m.id, m.client_id, m.version, m.entry, m.icon) == ("demo", "ext-demo", "1.2.0", "/", "icon.svg")
    assert m.consent.scopes == ("ext-data-read",)
    assert [(s.name, s.mode, s.scopes) for s in m.services] == [
        ("projects", "user", ("svc-projects-read",)),
        ("export", "service", ("svc-export-write",)),
    ]
    assert m.user_service_scopes == ("svc-projects-read",)
    assert m.all_scopes == ("ext-data-read", "svc-projects-read", "svc-export-write")
    assert m.service("projects").audience == "projects-api"
    assert m.name_localized["de"] == "Demo" and m.hosts == ("cdn.example.test",)
    assert m.client_auth == "private_key_jwt" and m.dev_port is None
    with pytest.raises(KeyError, match="declared: projects, export"):
        m.service("nope")


def test_display_is_in_app_unless_the_manifest_says_otherwise():
    assert parse_manifest(BASE).display == "in_app" and not parse_manifest(BASE).external
    external = parse_manifest(variant(extension__display="external"))
    assert external.display == "external" and external.external
    assert parse_manifest(variant(extension__display="in_app")).display == "in_app"


@pytest.mark.parametrize("value", ["popup", "External", "", True, 1, ["external"]])
def test_rule4_display_is_one_of_the_two_modes(value):
    assert error_paths(variant(extension__display=value)) == ["extension.display"]


def test_the_manifest_is_immutable():
    m = loads_manifest(MANIFEST)
    with pytest.raises(Exception):
        m.id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        m.name_localized["fr"] = "x"  # type: ignore[index]


def test_minimal_manifest_needs_no_sections():
    m = parse_manifest({"extension": BASE["extension"]})
    assert m.services == () and m.consent.scopes == () and m.description == ""


def test_an_empty_description_is_allowed_but_a_non_string_is_not():
    assert parse_manifest(variant(extension__description="")).description == ""
    assert error_paths(variant(extension__description=5)) == ["extension.description"]


def test_optional_keys():
    data = variant(
        extension__description="Reports", extension__min_app_version="1.0.0", extension__audience_roles=["analyst"],
        extension__client_auth="client_secret", extension__dev_port=8001, extension__hosts=["cdn.example.test", "a.b-c.example"],
        extension__description_localized={"de": "Berichte", "pt-BR": "Relatórios"},
    )
    m = parse_manifest(data)
    assert (m.description, m.min_app_version, m.audience_roles, m.client_auth, m.dev_port) == ("Reports", "1.0.0", ("analyst",), "client_secret", 8001)
    assert m.description_localized["pt-BR"] == "Relatórios"


# -- rule 1: required fields, id, version, entry -----------------------------------------------------


@pytest.mark.parametrize("key", ["id", "name", "version", "entry", "icon"])
def test_rule1_required_fields(key):
    assert error_paths(variant(**{f"extension__{key}": DELETE})) == [f"extension.{key}"]


def test_rule1_missing_extension_section():
    assert error_paths({}) == ["extension"]


@pytest.mark.parametrize("value", ["", "  ", 5, None, ["x"]])
def test_rule1_name_must_be_a_non_empty_string(value):
    assert error_paths(variant(extension__name=value)) == ["extension.name"]


@pytest.mark.parametrize(
    "ext_id",
    ["a", "ab", "Demo", "demo_x", "-demo", "demo-", "1demo", "demo.x", "d" * 41, "demo space", "démo", "demo\n"],
)
def test_rule1_invalid_ids(ext_id):
    assert error_paths(variant(extension__id=ext_id)) == ["extension.id"]


@pytest.mark.parametrize("ext_id", ["abc", "a1b", "my-ext", "d" * 40, "x-1-y"])
def test_rule1_valid_ids(ext_id):
    assert parse_manifest(variant(extension__id=ext_id)).id == ext_id


@pytest.mark.parametrize("version", ["1", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-", "1.2.3+build", "latest", "1.2.x", "1.2.3 ", "1.2.3\n"])
def test_rule1_invalid_versions(version):
    assert error_paths(variant(extension__version=version)) == ["extension.version"]


@pytest.mark.parametrize("version", ["0.0.1", "1.2.3", "10.20.30", "1.0.0-rc.1", "1.0.0-beta", "2.0.0-0", "01.2.3"])
def test_rule1_valid_versions(version):
    assert parse_manifest(variant(extension__version=version)).version == version


@pytest.mark.parametrize("entry", ["", "index.html", "//evil.test", "//evil.test/x", "http://evil.test/", "https://x", "javascript:alert(1)", "/\\evil.test"])
def test_rule1_invalid_entries(entry):
    assert error_paths(variant(extension__entry=entry)) == ["extension.entry"]


@pytest.mark.parametrize("entry", ["/", "/app", "/app/index.html?x=1", "/a/b/"])
def test_rule1_valid_entries(entry):
    assert parse_manifest(variant(extension__entry=entry)).entry == entry


# -- rule 2: scope names and duplicates --------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["", "Ext-Data", "ext_data", "1scope", "scope with space", "ext:data", "-x"])
def test_rule2_invalid_scope_names_in_consent(scope):
    assert error_paths(variant(consent__scopes=["ok-scope", scope])) == ["consent.scopes[1]"]


def test_rule2_invalid_scope_names_in_services():
    assert error_paths(variant(services__0__scopes=["fine", "Bad_Scope"])) == ["services[0].scopes[1]"]


def test_rule2_duplicates_within_consent():
    assert error_paths(variant(consent__scopes=["a", "a"])) == ["consent.scopes[1]"]


def test_rule2_duplicates_across_consent_and_services():
    assert error_paths(variant(services__0__scopes=["ext-data-read"])) == ["services[0].scopes[0]"]


def test_rule2_duplicates_between_services():
    assert error_paths(variant(services__1__scopes=["svc-projects-read"])) == ["services[1].scopes[0]"]


def test_rule2_scopes_must_be_lists():
    assert error_paths(variant(consent__scopes="ext-data-read")) == ["consent.scopes"]


# -- rule 3: services ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "Projects", "my-service", "1abc", "has space"])
def test_rule3_invalid_service_names(name):
    assert error_paths(variant(services__0__name=name)) == ["services[0].name"]


def test_rule3_service_names_are_unique():
    assert error_paths(variant(services__1__name="projects")) == ["services[1].name"]


@pytest.mark.parametrize("mode", ["", "User", "app", "both", 1])
def test_rule3_invalid_modes(mode):
    assert error_paths(variant(services__0__mode=mode)) == ["services[0].mode"]


@pytest.mark.parametrize("audience", ["", "  ", 5])
def test_rule3_audience_not_empty(audience):
    assert error_paths(variant(services__0__audience=audience)) == ["services[0].audience"]


def test_rule3_a_service_needs_at_least_one_scope():
    assert error_paths(variant(services__0__scopes=[])) == ["services[0].scopes"]


@pytest.mark.parametrize("key", ["name", "audience", "scopes", "mode"])
def test_rule3_service_fields_are_required(key):
    assert error_paths(variant(**{f"services__0__{key}": DELETE})) == [f"services[0].{key}"]


def test_rule3_services_must_be_an_array_of_tables():
    assert error_paths(variant(services={"name": "x"})) == ["services"]
    assert error_paths(variant(services=["x"])) == ["services[0]"]


# -- rule 4: optional fields ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["", "has space", 5, "-x", "1x", "x" * 65])
def test_rule4_audience_roles(role):
    assert error_paths(variant(extension__audience_roles=["good", role])) == ["extension.audience_roles[1]"]


@pytest.mark.parametrize("version", ["1", "x", 1, "1.2"])
def test_rule4_min_app_version_is_semver(version):
    assert error_paths(variant(extension__min_app_version=version)) == ["extension.min_app_version"]


@pytest.mark.parametrize("host", ["https://cdn.example.test", "cdn.example.test/path", "CDN.example.test", "cdn.example.test:8080", "", "-a.b", "a..b", "a b"])
def test_rule4_hosts_are_bare_lower_case_host_names(host):
    assert error_paths(variant(extension__hosts=["ok.example.test", host])) == ["extension.hosts[1]"]


@pytest.mark.parametrize("method", ["password", "", "PRIVATE_KEY_JWT", 1])
def test_rule4_client_auth(method):
    assert error_paths(variant(extension__client_auth=method)) == ["extension.client_auth"]


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, -1, "8000", 8000.5, True])
def test_rule4_dev_port_range(port):
    assert error_paths(variant(extension__dev_port=port)) == ["extension.dev_port"]


@pytest.mark.parametrize("port", [1024, 8000, 65535])
def test_rule4_dev_port_valid(port):
    assert parse_manifest(variant(extension__dev_port=port)).dev_port == port


# -- rule 5: localized tables -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["", "d", "deu", "DE", "de-de", "de_DE", "de-DEU"])
def test_rule5_language_codes(lang):
    assert error_paths(variant(extension__name_localized={lang: "Demo"})) == [f"extension.name_localized.{lang}"]


@pytest.mark.parametrize("text", ["", "   ", 5, None])
def test_rule5_texts_not_empty(text):
    assert error_paths(variant(extension__description_localized={"de": text})) == ["extension.description_localized.de"]


def test_rule5_localized_must_be_tables():
    assert error_paths(variant(extension__name_localized="Demo")) == ["extension.name_localized"]


# -- rule 6: unknown keys are errors --------------------------------------------------------------------------------------


def test_rule6_unknown_key_in_extension():
    assert error_paths(variant(extension__descripton="typo")) == ["extension.descripton"]


def test_rule6_unknown_key_in_consent():
    assert error_paths(variant(consent__scope=["x"])) == ["consent.scope"]


def test_rule6_unknown_key_in_a_service():
    assert error_paths(variant(services__0__scope=["x"])) == ["services[0].scope"]


def test_rule6_unknown_top_level_section():
    data = variant()
    data["service"] = []
    assert error_paths(data) == ["service"]


# -- rule 8: a link ---------------------------------------------------------------------------------------------------------------

LINK = {"extension": {"id": "demo-link", "name": "Demo link", "version": "1.0.0", "kind": "link", "entry": "https://shop.example.com/app?ref=app#top"}}


def link(**changes):
    """LINK with `extension` keys changed; a value of `DELETE` removes the key."""
    data = copy.deepcopy(LINK)
    for key, value in changes.items():
        if value is DELETE:
            del data["extension"][key]
        else:
            data["extension"][key] = value
    return data


def test_an_ordinary_extension_is_of_kind_extension():
    m = parse_manifest(BASE)
    assert m.kind == "extension" and not m.is_link
    assert parse_manifest(variant(extension__kind="extension")).kind == "extension"


def test_a_link_has_no_server_behind_it():
    m = parse_manifest(LINK)
    assert m.kind == "link" and m.is_link
    assert m.entry == "https://shop.example.com/app?ref=app#top"
    assert m.icon == ""  # optional for a link, and an address instead of a file
    assert m.display == "external" and m.external  # the only way a link is shown
    assert m.hosts == () and m.dev_port is None and m.consent.scopes == () and m.services == ()
    assert m.all_scopes == () and m.user_service_scopes == ()


def test_a_link_may_name_an_icon_on_the_host_of_its_entry():
    m = parse_manifest(link(icon="https://shop.example.com/static/icon.svg"))
    assert m.icon == "https://shop.example.com/static/icon.svg"


def test_a_link_keeps_the_optional_keys_that_make_sense_for_it():
    m = parse_manifest(link(
        description="Our shop", min_app_version="1.0.0", audience_roles=["advisor"],
        name_localized={"de": "Unser Shop"}, description_localized={"de": "Der Shop"}, display="external",
    ))
    assert (m.description, m.min_app_version, m.audience_roles) == ("Our shop", "1.0.0", ("advisor",))
    assert m.name_localized["de"] == "Unser Shop" and m.description_localized["de"] == "Der Shop"


def test_a_link_may_leave_the_tables_empty():
    data = {**link(), "consent": {"scopes": []}, "services": []}
    assert parse_manifest(data).is_link


@pytest.mark.parametrize("kind", ["Link", "", "links", True, 1, ["link"]])
def test_rule8_kind_is_one_of_the_two_kinds(kind):
    assert error_paths(variant(extension__kind=kind)) == ["extension.kind"]


@pytest.mark.parametrize("entry", [
    "/", "/app", "shop.example.com", "//shop.example.com/", "http://shop.example.com/", "ftp://shop.example.com/",
    "javascript:alert(1)", "https://", "https:///path", "https://user@shop.example.com/", "https://user:pw@shop.example.com/",
    "https://shop.example.com/a b", "https://shop.example.com/\\x", "https://shöp.example.com/",
    "https://shop.example.com:99999/", "https://" + "a" * 2050 + ".example.com/", "",
])
def test_rule8_a_links_entry_is_an_absolute_https_address(entry):
    assert error_paths(link(entry=entry)) == ["extension.entry"]


@pytest.mark.parametrize("entry", ["http://127.0.0.1:8080/x", "http://localhost/", "http://[::1]:9000/", "https://shop.example.com:8443/a/b?c=d"])
def test_rule8_http_is_for_this_machine_only(entry):
    assert parse_manifest(link(entry=entry)).entry == entry


def test_rule8_a_link_needs_its_entry():
    assert error_paths(link(entry=DELETE)) == ["extension.entry"]


@pytest.mark.parametrize("icon", ["icon.svg", "/icon.svg", "http://shop.example.com/icon.svg", "https://cdn.example.com/icon.svg", "https://", "", 5])
def test_rule8_a_links_icon_is_an_address_on_the_host_of_its_entry(icon):
    assert error_paths(link(icon=icon)) == ["extension.icon"]


@pytest.mark.parametrize("key, value", [("client_auth", "private_key_jwt"), ("client_auth", "nonsense"), ("dev_port", 8100),
                                        ("hosts", []), ("hosts", ["cdn.example.test"])])
def test_rule8_a_link_refuses_what_belongs_to_a_server(key, value):
    assert error_paths(link(**{key: value})) == [f"extension.{key}"]


def test_rule8_a_link_is_always_shown_in_the_system_browser():
    assert error_paths(link(display="in_app")) == ["extension.display"]


def test_rule8_a_link_asks_for_no_scopes_and_calls_no_services():
    assert error_paths({**link(), "consent": {"scopes": ["ext-data-read"]}}) == ["consent.scopes"]
    assert error_paths({**link(), "services": BASE["services"]}) == ["services"]


def test_rule8_the_server_rules_still_apply_to_a_link_where_they_make_sense():
    assert error_paths(link(id="Bad", version="x")) == ["extension.id", "extension.version"]
    assert error_paths(link(min_app_version="x")) == ["extension.min_app_version"]


def test_rule8_kind_is_not_a_key_of_the_other_tables():
    assert error_paths({**link(), "consent": {"kind": "link"}}) == ["consent.kind"]


# -- reading ---------------------------------------------------------------------------------------------------------------------


def test_all_errors_are_reported_together():
    data = variant(extension__id="BAD", extension__version="x", services__0__mode="nope", consent__scopes=["Bad"])
    assert sorted(error_paths(data)) == sorted(["extension.id", "extension.version", "services[0].mode", "consent.scopes[0]"])


def test_error_shape_and_message():
    with pytest.raises(ManifestError) as err:
        parse_manifest(variant(extension__id="BAD"))
    (error,) = err.value.errors
    assert set(error) == {"path", "message"} and error["path"] == "extension.id"
    assert "extension.id" in str(err.value)


def test_invalid_toml_is_a_manifest_error():
    with pytest.raises(ManifestError) as err:
        loads_manifest("[extension\nid = ")
    assert err.value.errors[0]["path"] == "" and "TOML" in err.value.errors[0]["message"]


def test_load_manifest_from_path_and_text(tmp_path):
    path = tmp_path / "extension.toml"
    path.write_text(MANIFEST)
    from_path = load_manifest(path)
    assert from_path.base_dir == tmp_path.resolve()
    assert load_manifest(str(path)).id == "demo"
    from_text = load_manifest(MANIFEST)
    assert from_text.base_dir is None and from_text.id == "demo"
    assert load_manifest(MANIFEST.encode()).id == "demo"


def test_missing_file_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.toml")
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(str(tmp_path / "nope.toml"))


def test_non_table_input():
    with pytest.raises(ManifestError):
        parse_manifest([])  # type: ignore[arg-type]
