"""Letting a phone in without letting the network in.

Every other test in this suite runs against a server whose security rests on
being unreachable: bound to loopback, token in the page, done. These are the
tests for the day that stops being true, and they are mostly about the failure
that would not look like one — a phone that works, on a machine that is now
also serving a terminal to everything on the Wi-Fi.
"""
import pytest

from carrot import config, pairing, security
from carrot import app as app_mod


@pytest.fixture
def paired(client, isolated_db):
    """A phone that has been through the front door properly."""
    pairing.open_window()
    code = pairing.window_state()["code"]
    return pairing.claim(code, name="Test phone", user_agent="pytest")


class TestTheTokenStopsAtTheMachine:
    """The one property everything else here depends on.

    `/` has to be public — the shell loads before anything can authenticate —
    and it carries the session token in a meta tag. That was safe while
    loopback was the only way to ask for it. It is the whole vulnerability the
    moment Carrot listens on a network.
    """

    def test_loopback_still_gets_the_token(self, client):
        """The desktop app must be completely unaffected by any of this."""
        html = client.get("/").text
        assert security.session_token() in html

    def test_an_off_machine_request_gets_the_page_without_it(self, client):
        """Not a 403: the phone is *supposed* to load the app. It just does not
        get handed the keys on the way in."""
        was = app_mod.BOUND_HOST
        app_mod.note_bound_host("0.0.0.0")
        try:
            html = client.get("/").text
        finally:
            app_mod.note_bound_host(was)
        assert security.session_token() not in html
        assert "carrot" in html.lower()

    def test_the_check_reads_the_socket_not_a_header(self):
        """Whatever is in front of this server is sometimes a tunnel somebody
        else runs, and `X-Forwarded-For` is a header the caller can set. The
        peer address comes off the connection or the answer is no."""
        with open(app_mod.__file__, encoding="utf-8") as handle:
            body = handle.read()
        start = body.index("def _is_loopback")
        function = body[start:body.index("\n\n\n", start)]
        assert "request.client" in function
        assert "request.headers" not in function


class TestTheCode:
    def test_a_code_is_needed(self, isolated_db):
        pairing.open_window()
        with pytest.raises(pairing.PairingRefused):
            pairing.claim("WRONG1", name="not me")

    def test_pairing_is_refused_when_no_window_is_open(self, isolated_db):
        pairing.close_window()
        with pytest.raises(pairing.PairingRefused) as refused:
            pairing.claim("ABC123")
        assert "not open" in str(refused.value)

    def test_five_wrong_guesses_shuts_the_window(self, isolated_db):
        """Not throttled — shut. A window that only slows down under repeated
        failure is one that tells an attacker their guessing is free."""
        pairing.open_window()
        for _ in range(pairing.MAX_ATTEMPTS):
            with pytest.raises(pairing.PairingRefused):
                pairing.claim("NOPE99")
        assert pairing.window_state()["open"] is False

    def test_a_code_is_spent_on_first_use(self, isolated_db):
        """Otherwise one glance at the screen pairs a second device."""
        pairing.open_window()
        code = pairing.window_state()["code"]
        pairing.claim(code, name="first")
        assert pairing.window_state()["open"] is False
        with pytest.raises(pairing.PairingRefused):
            pairing.claim(code, name="second")

    def test_the_alphabet_has_no_ambiguous_characters(self):
        """This is read off one screen and typed into another, usually badly."""
        for char in "IO01":
            assert char not in pairing.CODE_ALPHABET


class TestTheDeviceToken:
    def test_it_opens_the_api(self, client, paired):
        answer = client.get("/api/activity",
                            headers={security.TOKEN_HEADER: paired["token"]})
        assert answer.status_code == 200

    def test_it_is_not_stored_in_the_clear(self, paired, isolated_db):
        """A database that is a keyring is a backup that is a keyring."""
        for device in pairing.list_devices():
            assert paired["token"] not in str(device)
        conn = __import__("carrot.database", fromlist=["get_db"]).get_db()
        rows = conn.execute("SELECT token_hash FROM paired_devices").fetchall()
        conn.close()
        assert rows and all(paired["token"] not in row["token_hash"] for row in rows)

    def test_revoking_ends_it_immediately(self, client, paired):
        device_id = paired["device"]["id"]
        assert pairing.revoke(device_id) is True
        answer = client.get("/api/activity",
                            headers={security.TOKEN_HEADER: paired["token"]})
        assert answer.status_code == 401

    def test_an_invented_token_opens_nothing(self, client, isolated_db):
        answer = client.get("/api/activity",
                            headers={security.TOKEN_HEADER: "not-a-real-token"})
        assert answer.status_code == 401

    def test_one_phones_token_is_not_anothers(self, isolated_db):
        pairing.open_window()
        first = pairing.claim(pairing.window_state()["code"], name="one")
        pairing.open_window()
        second = pairing.claim(pairing.window_state()["code"], name="two")
        assert first["token"] != second["token"]
        pairing.revoke(first["device"]["id"])
        assert pairing.device_token_valid(second["token"]) is True

    def test_the_session_token_still_works_alongside_it(self, client, paired):
        answer = client.get("/api/activity",
                            headers={security.TOKEN_HEADER: security.session_token()})
        assert answer.status_code == 200


class TestTheQrCarriesTheAddressAndNotTheCode:
    """Scan for *where*, type for *who*. One scan instead of a scan and six
    characters would mean writing a live credential into the phone's address
    bar and history, which is the thing this codebase already refused to do
    with the SSE token."""

    def test_the_code_is_not_in_the_encoded_url(self, client, isolated_db):
        pairing.open_window()
        code = pairing.window_state()["code"]
        addresses = app_mod.reachable_addresses()
        if not addresses:
            pytest.skip("this machine has no non-loopback address")
        answer = client.get("/api/pair/qr",
                            params={"address": addresses[0]["address"]}).json()
        assert code not in answer["url"]
        assert code not in answer["svg"]

    def test_an_address_this_machine_does_not_have_is_refused(self, client):
        """The parameter ends up inside an SVG the desktop injects into its own
        page, so it is matched against a list we computed rather than echoed."""
        answer = client.get("/api/pair/qr", params={"address": "10.9.9.9"})
        assert answer.status_code == 400

    def test_a_missing_encoder_costs_the_qr_and_nothing_else(self, monkeypatch):
        """The address is printed underneath it in text. Losing the convenience
        must not lose the ability to pair."""
        import builtins
        real_import = builtins.__import__

        def no_segno(name, *args, **kwargs):
            if name == "segno":
                raise ImportError("no segno here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_segno)
        assert pairing.qr_svg("http://10.0.0.5:8181") == ""


class TestAnyBrowserIsTheClient:
    """There is no "mobile app" and no separate web client to build. The thing
    served over the network is the same app the desktop runs, and a phone, a
    Chromebook and another laptop are all just browsers that have paired."""

    def test_nothing_electron_only_is_called_unguarded(self):
        """A shell API called without a fallback is a feature that works on the
        desktop and throws in every browser that reaches it."""
        from pathlib import Path
        web = Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
        guards = ("window.carrot?.", "window.carrot &&",
                  "window.carrotAPI?.", "window.carrotAPI &&")
        for path in web.glob("*.js"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for line_no, line in enumerate(lines, 1):
                code = line.split("//", 1)[0]
                if "window.carrot" not in code:
                    continue
                # The guard opens a block, so the call it protects can be
                # several lines below it — theme.js reads the computed palette
                # in between. A short lookback keeps this honest about real
                # unguarded calls without inventing failures for guarded ones.
                window_above = "\n".join(lines[max(0, line_no - 9):line_no])
                assert any(guard in window_above for guard in guards), \
                    f"{path.name}:{line_no} calls the Electron bridge unguarded"

    def test_a_chromebook_is_named_as_one(self):
        """Its user agent says X11, Linux and sometimes Android too, so the
        CrOS test has to come first — a list that calls somebody's laptop an
        Android tablet is one they stop trusting to say what to sign out."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "pair.js").read_text(encoding="utf-8")
        assert js.index("CrOS") < js.index("/Android/i")


class TestWhatTheDeskCanDoAndThePhoneCannot:
    """The token check treats a paired device and the desktop as equals, which
    is right for *using* Carrot and wrong for *administering* it. Without this
    split a stolen phone token could enrol more devices — surviving the
    revocation of the phone it came from, which is the one thing revocation
    exists to prevent."""

    def test_opening_the_window_needs_the_session(self, client):
        assert "/api/pair/open" not in security.PUBLIC_PATHS
        assert "/api/devices" not in security.PUBLIC_PATHS

    def test_only_pairing_and_its_requirements_are_public(self):
        public = {p for p in security.PUBLIC_PATHS if p.startswith("/api/pair")}
        assert public == {"/api/pair", "/api/pair/requirements"}

    @pytest.mark.parametrize("method,path", [
        ("post", "/api/pair/open"),
        ("post", "/api/pair/close"),
        ("get", "/api/pair/state"),
        ("get", "/api/devices"),
        ("post", "/api/pair/credentials"),
        ("post", "/api/remote-access"),
    ])
    def test_a_paired_device_is_refused_the_controls(self, client, paired,
                                                     method, path):
        answer = getattr(client, method)(
            path, headers={security.TOKEN_HEADER: paired["token"]},
            **({"json": {}} if method == "post" else {}))
        assert answer.status_code == 403, path

    def test_a_paired_device_cannot_enrol_another(self, client, paired):
        """The escalation this exists to stop, stated as the thing it is."""
        opened = client.post("/api/pair/open",
                             headers={security.TOKEN_HEADER: paired["token"]})
        assert opened.status_code == 403
        assert pairing.window_state()["open"] is False

    def test_a_paired_device_cannot_revoke_its_siblings(self, client, paired):
        pairing.open_window()
        other = pairing.claim(pairing.window_state()["code"], name="the other phone")
        answer = client.delete("/api/devices/" + other["device"]["id"],
                               headers={security.TOKEN_HEADER: paired["token"]})
        assert answer.status_code == 403
        assert pairing.device_token_valid(other["token"]) is True

    def test_the_desktop_can_do_all_of_it(self, client, isolated_db):
        assert client.post("/api/pair/open").status_code == 200
        assert client.get("/api/devices").status_code == 200


class TestTheSharedSignIn:
    """Tailscale says the two machines are on one private network; the pairing
    code says somebody is standing at the host. Both are facts about a *place*.
    This is the one that is about a person."""

    def test_none_set_means_nothing_to_match(self, isolated_db):
        assert pairing.credentials_match("", "") is True
        assert pairing.credential_state()["required"] is False

    def test_pairing_needs_it_once_it_is_set(self, isolated_db):
        pairing.set_credentials("adarz", "correct-horse")
        pairing.open_window()
        with pytest.raises(pairing.PairingRefused) as refused:
            pairing.claim(pairing.window_state()["code"], password="wrong")
        assert "name and password" in str(refused.value)

    def test_the_right_pair_gets_in(self, isolated_db):
        pairing.set_credentials("adarz", "correct-horse")
        pairing.open_window()
        paired = pairing.claim(pairing.window_state()["code"],
                               username="adarz", password="correct-horse")
        assert paired["token"]

    def test_a_wrong_password_costs_an_attempt(self, isolated_db):
        """Otherwise the code could be brute-forced with the password left
        blank, and the five-guess limit would protect only half the door."""
        pairing.set_credentials("adarz", "correct-horse")
        pairing.open_window()
        for _ in range(pairing.MAX_ATTEMPTS):
            with pytest.raises(pairing.PairingRefused):
                pairing.claim(pairing.window_state()["code"], password="wrong")
        assert pairing.window_state()["open"] is False

    def test_the_password_is_not_recoverable_from_what_is_stored(self, isolated_db):
        pairing.set_credentials("adarz", "correct-horse")
        stored = str(config.get_config().get(pairing.CREDENTIAL_KEY))
        assert "correct-horse" not in stored
        assert pairing.credential_state().get("hash") is None

    def test_it_is_slow_to_attack_unlike_the_device_tokens(self):
        """A device token is 32 bytes from `secrets` and needs no KDF. A
        password is short and human-chosen, so the stored form has to be
        expensive."""
        assert pairing.PBKDF2_ROUNDS >= 200_000

    def test_a_short_password_is_refused(self, isolated_db):
        with pytest.raises(ValueError):
            pairing.set_credentials("adarz", "abc")

    def test_the_requirements_route_never_leaks_the_secret(self, client, isolated_db):
        pairing.set_credentials("adarz", "correct-horse")
        body = client.get("/api/pair/requirements").json()
        assert body["credentials_required"] is True
        assert "correct-horse" not in str(body)
        assert "hash" not in str(body)


class TestTheTailnetIsTheOnlyWayIn:
    """The LAN was offered first and should not have been. Carrot speaks plain
    HTTP, so on a local network the device token crosses the air in the clear —
    and the app it opens runs shell commands. "Same Wi-Fi" sounds like a
    boundary; at a school or an office it is not one."""

    def test_only_a_tailnet_address_is_ever_offered(self, monkeypatch):
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "100.87.14.3")
        rows = app_mod.reachable_addresses()
        assert [row["kind"] for row in rows] == ["tailscale"]

    def test_no_tailnet_means_no_address_at_all(self, monkeypatch):
        """Rather than falling back to something that happens to work."""
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "")
        assert app_mod.reachable_addresses() == []

    def test_turning_it_on_without_tailscale_is_refused(self, client, monkeypatch,
                                                        isolated_db):
        """The alternative is a switch that says yes, binds to whatever is
        available, and quietly puts a shell on the school Wi-Fi."""
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "")
        answer = client.post("/api/remote-access", json={"enabled": True})
        assert answer.status_code == 409
        assert "Tailscale" in answer.json()["detail"]
        assert config.get_config().get("server_host") != "0.0.0.0"

    def test_it_binds_the_interface_rather_than_everything(self, client, monkeypatch,
                                                           isolated_db):
        """`0.0.0.0` would put the port on every network this machine is on.
        Binding one interface is the operating system refusing the connection
        instead of us, which is a much better place for that decision."""
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "100.87.14.3")
        client.post("/api/remote-access", json={"enabled": True})
        assert config.get_config().get("server_host") == "100.87.14.3"

    @pytest.mark.parametrize("address,allowed", [
        ("100.64.0.1", True), ("100.127.255.254", True),
        ("100.63.0.1", False), ("100.128.0.1", False),
        ("192.168.1.20", False), ("10.0.0.5", False), ("8.8.8.8", False),
    ])
    def test_the_tailnet_range_is_what_it_says(self, address, allowed):
        assert app_mod._is_tailnet_address(address) is allowed


class TestTheNetworkFilter:
    """Second layer. The bind is what actually keeps the port off the local
    network; this is what stands between a hand-edited `server_host` and a
    shell on the school Wi-Fi, and it is the only check that applies to
    `/api/pair`, which has to answer without a token."""

    class _Peer:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()

    @pytest.mark.parametrize("host,allowed", [
        ("127.0.0.1", True),        # the desktop app talking to itself
        ("::1", True),
        ("100.87.14.3", True),      # a device on your tailnet
        ("192.168.1.50", False),    # the same Wi-Fi, which is not a boundary
        ("10.0.0.7", False),
        ("203.0.113.9", False),
    ])
    def test_only_this_machine_and_the_tailnet(self, host, allowed):
        assert app_mod._from_an_allowed_network(self._Peer(host)) is allowed

    def test_a_peer_it_cannot_read_is_refused(self):
        """"I could not tell where this came from" is not a reason to hand over
        a terminal."""
        assert app_mod._from_an_allowed_network(self._Peer("")) is False

    def test_it_does_not_apply_while_bound_to_loopback(self):
        """There is no untrusted network to filter, and a check that can lock
        somebody out of their own desktop over an unparseable peer is a bigger
        failure than the one it prevents."""
        with open(app_mod.__file__, encoding="utf-8") as handle:
            body = handle.read()
        assert "if listening_off_machine() and not _from_an_allowed_network" in body


class TestItStillStartsWhenTailscaleIsNot:
    """A tailnet address belongs to no interface once Tailscale stops, so
    binding to it fails and uvicorn exits. Carrot not launching because a
    network tool is not running is an unacceptable way for an offline-first
    app to fail."""

    def test_an_unbindable_host_falls_back_to_loopback(self, monkeypatch, isolated_db):
        config.set_config("remote_access", True)
        config.set_config("server_host", "100.87.14.3")
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "")
        assert app_mod.resolve_bind_host() == "127.0.0.1"

    def test_and_says_why(self, monkeypatch, isolated_db):
        """Otherwise the phone stops connecting and nothing anywhere explains
        it."""
        config.set_config("remote_access", True)
        config.set_config("server_host", "100.87.14.3")
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "")
        app_mod.resolve_bind_host()
        assert "Tailscale is not running" in app_mod.BIND_FALLBACK_REASON

    def test_a_live_tailnet_address_is_kept(self, monkeypatch, isolated_db):
        config.set_config("remote_access", True)
        config.set_config("server_host", "100.87.14.3")
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "100.87.14.3")
        assert app_mod.resolve_bind_host() == "100.87.14.3"
        assert app_mod.BIND_FALLBACK_REASON == ""

    def test_a_leftover_address_with_the_feature_off_is_ignored(self, isolated_db):
        config.set_config("remote_access", False)
        config.set_config("server_host", "100.87.14.3")
        assert app_mod.resolve_bind_host() == "127.0.0.1"


class TestBeingReachableIsItsOwnDecision:
    def test_it_is_off_by_default(self, isolated_db):
        assert app_mod.remote_access_state()["enabled"] is False
        assert app_mod.remote_access_state()["listening_off_machine"] is False

    def test_turning_it_on_says_a_restart_is_needed(self, client, monkeypatch,
                                                    isolated_db):
        monkeypatch.setattr(app_mod, "tailnet_address", lambda: "100.87.14.3")
        state = client.post("/api/remote-access", json={"enabled": True}).json()
        assert state["enabled"] is True
        # The socket has not moved; claiming otherwise is the kind of lie that
        # costs somebody an afternoon.
        assert state["restart_required"] is True

    def test_turning_it_off_shuts_any_open_code(self, client, isolated_db):
        pairing.open_window()
        client.post("/api/remote-access", json={"enabled": False})
        assert pairing.window_state()["open"] is False
