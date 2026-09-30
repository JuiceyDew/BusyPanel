{
  description = "busypanel - client videos, monthly invoices and expenses";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
    ...
  }: let
    inherit (nixpkgs) lib;
    systems = ["x86_64-linux" "aarch64-linux"];
    eachSystem = f: lib.genAttrs systems (system: f (import nixpkgs {inherit system;}));

    # A Python environment built from uv.lock. The workspace is the project
    # itself; overrides come from the build-system set, then the uv overlay.
    mkPythonSet = pkgs: let
      workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};
      # "wheel" keeps builds fast and avoids compiling from source. The
      # alternative -- building from nixpkgs' python312Packages -- is not an
      # option here: those derivations run their own check phases, and a failing
      # check in one transitive dependency fails the whole build.
      overlay = workspace.mkPyprojectOverlay {sourcePreference = "wheel";};
    in
      (pkgs.callPackage pyproject-nix.build.packages {
        python = pkgs.python312;
      })
      .overrideScope (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
        ]
      );
  in {
    packages = eachSystem (pkgs: let
      pythonSet = mkPythonSet pkgs;
      env = pythonSet.mkVirtualEnv "busypanel-env" {
        busypanel = [];
      };
      # The venv is named "busypanel-env", so `nix run` would look for a binary
      # of that name. Point it at the real console script instead.
      app = env.overrideAttrs (old: {
        meta = (old.meta or {}) // {mainProgram = "busypanel";};
      });
    in {
      default = app;
      app = app;
    });

    # `nix develop` -- test extras only, so the shell stays reliable.
    devShells = eachSystem (pkgs: let
      pythonSet = mkPythonSet pkgs;
      env = pythonSet.mkVirtualEnv "busypanel-dev" {
        busypanel = ["dev"];
      };
    in {
      default = pkgs.mkShell {
        packages = [env pkgs.uv pkgs.git];
        env = {
          UV_NO_SYNC = "1";
          UV_PYTHON = pythonSet.python.interpreter;
          UV_PYTHON_DOWNLOADS = "never";
        };
        shellHook = ''
          unset PYTHONPATH
          echo "busypanel dev shell ready"
        '';
      };
    });

    # A booted-VM check of the NixOS module. `nix flake check` then proves the
    # service actually starts, hardens its state directory and serves the UI --
    # not merely that the options evaluate. This is the test that would have
    # caught a wrong ExecStart or a missing StateDirectory.
    checks = eachSystem (pkgs: {
      service = pkgs.testers.nixosTest {
        name = "busypanel-service";

        nodes.machine = {...}: {
          imports = [self.nixosModules.default];
          services.busypanel = {
            enable = true;
            host = "127.0.0.1";
            port = 8090;
            openFirewall = true;
          };
          # Left at the module default (no password), so the check exercises the
          # ungated path. The gate itself is covered by the offline test suite.
          system.stateVersion = "26.05";
          virtualisation.memorySize = 2048;
        };

        testScript = ''
          machine.wait_for_unit("busypanel.service")
          machine.wait_for_open_port(8090)

          # The health probe answers.
          assert "true" in machine.succeed("curl -fsS localhost:8090/health")

          # Every screen renders.
          assert "Uninvoiced" in machine.succeed("curl -fsS localhost:8090/")
          assert "Add a client" in machine.succeed("curl -fsS localhost:8090/clients")
          assert "Invoices" in machine.succeed("curl -fsS localhost:8090/invoices")
          assert "Summary" in machine.succeed("curl -fsS localhost:8090/summary")

          # The state directory is real, private, and owned by the service user.
          machine.succeed("test -d /var/lib/busypanel")
          machine.succeed("stat -c '%U %a' /var/lib/busypanel | grep -q 'busypanel 700'")

          # A full round trip through the running service: client -> video ->
          # monthly invoice, and the money lands on the invoice.
          machine.succeed("curl -fsS -X POST localhost:8090/clients -d 'name=Acme&video_rate=200' -o /dev/null")
          machine.succeed("curl -fsS -X POST localhost:8090/videos -d 'client_id=1&shot_on=2026-09-03&title=One&rate=200' -o /dev/null")
          machine.succeed("curl -fsS -X POST localhost:8090/videos -d 'client_id=1&shot_on=2026-09-14&title=Two&rate=350' -o /dev/null")

          # The row dialog is server-rendered: ?edit=<id> carries the row's
          # values and posts back to that row, which is the no-JavaScript path.
          edited = machine.succeed("curl -fsS 'localhost:8090/clients?edit=1'")
          assert 'id="dlg-client" open' in edited, edited[:800]
          assert 'action="/clients/1"' in edited, edited[:800]

          # A client can carry its own payment terms, and the invoice it creates
          # comes due that many days after the issue date.
          machine.succeed("curl -fsS -X POST localhost:8090/clients/1 -d 'name=Acme&video_rate=200&payment_terms=7' -o /dev/null")
          assert "7d" in machine.succeed("curl -fsS localhost:8090/clients")

          # An unbilled video can be edited through its own POST route.
          machine.succeed("curl -fsS -X POST localhost:8090/videos/1 -d 'client_id=1&shot_on=2026-09-03&title=Renamed&rate=250' -o /dev/null")
          assert "Renamed" in machine.succeed("curl -fsS localhost:8090/videos")

          machine.succeed("curl -fsS -X POST localhost:8090/invoices/monthly -d 'client_id=1&month=2026-09' -o /dev/null")

          printed = machine.succeed("curl -fsS localhost:8090/invoices/1/print")
          assert "Acme" in printed, printed[:800]
          assert "$600.00" in printed, printed[:800]

          # A second client proves the two new behaviours without disturbing the
          # Acme flow above: a video can be identified by its link alone, and
          # billing is a date range rather than a calendar month.
          machine.succeed("curl -fsS -X POST localhost:8090/clients -d 'name=Beta&video_rate=150' -o /dev/null")
          machine.succeed("curl -fsS -X POST localhost:8090/videos -d 'client_id=2&shot_on=2026-09-05&title=&link=https://youtu.be/abc&rate=150' -o /dev/null")
          machine.succeed("curl -fsS -X POST localhost:8090/videos -d 'client_id=2&shot_on=2026-10-05&title=October&rate=150' -o /dev/null")
          assert "youtu.be/abc" in machine.succeed("curl -fsS localhost:8090/videos")

          machine.succeed("curl -fsS -X POST localhost:8090/invoices/monthly -d 'client_id=2&start=2026-09-01&end=2026-09-30' -o /dev/null")
          sept = machine.succeed("curl -fsS localhost:8090/invoices/2/print")
          assert "youtu.be/abc" in sept, sept[:800]
          assert 'href="https://youtu.be/abc"' in sept, sept[:800]
          # Only the range's video: October's is still outstanding.
          assert "October" not in sept, sept[:800]
          assert "$150.00" in sept, sept[:800]
          assert "October" in machine.succeed("curl -fsS 'localhost:8090/videos?q=October'")

          # The batch can go on one line instead of one line per video.
          machine.succeed("curl -fsS -X POST localhost:8090/videos -d 'client_id=2&shot_on=2026-10-06&title=Extra&rate=100' -o /dev/null")
          machine.succeed("curl -fsS -X POST localhost:8090/invoices/monthly -d 'client_id=2&group=1&label=October+batch' -o /dev/null")
          batch = machine.succeed("curl -fsS localhost:8090/invoices/3/print")
          assert "October batch" in batch, batch[:800]
          assert "$250.00" in batch, batch[:800]
          assert "October — Oct 5" not in batch, batch[:800]

          # A note written on the invoice reaches the printed document.
          machine.succeed("curl -fsS -X POST localhost:8090/invoices/1/notes -d 'notes=Quoted before the rate rise' -o /dev/null")
          assert "Quoted before the rate rise" in machine.succeed("curl -fsS localhost:8090/invoices/1/print")

          # The list screens search, and the client has a detail page.
          assert "Renamed" in machine.succeed("curl -fsS 'localhost:8090/videos?q=Renamed'")
          assert "2026-0001" in machine.succeed("curl -fsS 'localhost:8090/invoices?q=2026-0001'")
          detail = machine.succeed("curl -fsS localhost:8090/clients/1")
          assert "At a glance" in detail, detail[:800]
          assert "$600.00" in detail, detail[:800]
          assert "404" in machine.succeed(
            "curl -sS -o /dev/null -w '%{http_code}' localhost:8090/clients/999"
          )

          # The books export as CSV, in plain cents for a bookkeeper.
          expenses_csv = machine.succeed("curl -fsS localhost:8090/export/expenses.csv")
          assert expenses_csv.startswith("Spent on,Vendor,Category,Client,Amount,Deductible,Notes"), expenses_csv[:200]
          invoices_csv = machine.succeed("curl -fsS localhost:8090/export/invoices.csv")
          assert "Number,Client,Kind" in invoices_csv.splitlines()[0], invoices_csv[:200]
          assert "600.00" in invoices_csv and "$" not in invoices_csv, invoices_csv[:400]

          # A billed video is the invoice's record and refuses further edits.
          assert "400" in machine.succeed(
            "curl -sS -o /dev/null -w '%{http_code}' -X POST localhost:8090/videos/1 "
            "-d 'client_id=1&shot_on=2026-09-03&title=Hacked&rate=999'"
          )
          assert "Hacked" not in machine.succeed("curl -fsS localhost:8090/videos")

          # The books survive a restart, which is what StateDirectory is for.
          machine.succeed("systemctl restart busypanel.service")
          machine.wait_for_open_port(8090)
          assert "2026-0001" in machine.succeed("curl -fsS localhost:8090/invoices")
        '';
      };
    });

    # NixOS module: a hardened systemd service with a state directory for the
    # database and the settings.json the UI writes.
    nixosModules.default = {
      config,
      lib,
      pkgs,
      ...
    }: let
      cfg = config.services.busypanel;

      # Fold the authPasswordFile shorthand into the generic credential map,
      # without letting it override an explicit credentials.AUTH_PASSWORD.
      credentials =
        lib.filterAttrs (_: v: v != null) cfg.credentials
        // lib.optionalAttrs (!(cfg.credentials ? AUTH_PASSWORD) && cfg.authPasswordFile != null) {
          AUTH_PASSWORD = cfg.authPasswordFile;
        };
      credentialLines = lib.mapAttrsToList (name: path: "${name}:${path}") credentials;
      credentialNames = lib.attrNames credentials;
    in {
      options.services.busypanel = {
        enable = lib.mkEnableOption "busypanel web UI";

        package = lib.mkOption {
          type = lib.types.package;
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          description = "The busypanel package to run.";
        };

        host = lib.mkOption {
          type = lib.types.str;
          default = "0.0.0.0";
          description = ''
            Bind address. Defaults to 0.0.0.0 so the UI is reachable from the
            LAN. The UI holds client bills and, once set, gates itself behind one
            password -- set `authPasswordFile` or `auth_password` before exposing
            it beyond a trusted subnet.
          '';
        };

        port = lib.mkOption {
          type = lib.types.port;
          default = 8090;
        };

        stateDir = lib.mkOption {
          type = lib.types.path;
          default = "/var/lib/busypanel";
          description = ''
            Where the database and settings live. Realised as a systemd
            `StateDirectory` owned by `user`/`group`, so it is created on first
            start and keeps the books private.
          '';
        };

        user = lib.mkOption {
          type = lib.types.str;
          default = "busypanel";
        };

        group = lib.mkOption {
          type = lib.types.str;
          default = "busypanel";
        };

        environmentFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            Optional file of Environment= lines. Keys set here override anything
            the web UI stored.
          '';
        };

        authPasswordFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          example = "/run/secrets/busypanel-password";
          description = ''
            File containing the single password that gates the web UI, so it is
            kept out of the Nix store. Shorthand for `credentials.AUTH_PASSWORD`.
            Loaded with systemd `LoadCredential` and read by `auth_password` at
            startup. When null and no password is stored through the UI, the
            panel is open to anyone who can reach `port`.
          '';
        };

        credentials = lib.mkOption {
          type = lib.types.attrsOf lib.types.path;
          default = {};
          example = {
            AUTH_PASSWORD = "/run/secrets/busypanel-password";
          };
          description = ''
            Secrets delivered to the service as systemd credentials, mapped
            `ENV_VAR_NAME = path-to-file`. Each file is read into the matching
            environment variable, so the value never lands in the
            world-readable Nix store. These take precedence over anything stored
            in the web UI, matching the usual environment-over-settings rule.

            The keys are the pydantic field names upper-cased, which is exactly
            what `busypanel/config.py` reads from the environment.
          '';
        };

        openFirewall = lib.mkOption {
          type = lib.types.bool;
          default = false;
        };
      };

      config = lib.mkIf cfg.enable {
        users.users.${cfg.user} = {
          isSystemUser = true;
          group = cfg.group;
          home = cfg.stateDir;
        };
        users.groups.${cfg.group} = {};

        networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [cfg.port];

        systemd.services.busypanel = {
          description = "busypanel web UI";
          wantedBy = ["multi-user.target"];
          after = ["network-online.target"];
          wants = ["network-online.target"];

          # `%S` expands to the state root that matches StateDirectory. Deriving
          # the path here rather than hardcoding cfg.stateDir keeps the two in
          # sync if an operator changes stateDir to something outside /var/lib.
          environment.BUSYPANEL_STATE_DIR = "%S/busypanel";

          serviceConfig = {
            ExecStart = "${cfg.package}/bin/busypanel web --host ${cfg.host} --port ${toString cfg.port}";
            User = cfg.user;
            Group = cfg.group;
            EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
            StateDirectory = "busypanel";
            StateDirectoryMode = "0700";
            WorkingDirectory = cfg.stateDir;

            # A homelab panel that stays up across reboots and transient failures.
            Restart = "on-failure";
            RestartSec = 10;

            # Hardening: the service needs only its own state dir and network.
            NoNewPrivileges = true;
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectSystem = "strict";
            ProtectHome = true;
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectControlGroups = true;
            RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX"];
            RestrictNamespaces = true;
            LockPersonality = true;
            RestrictRealtime = true;
            SystemCallArchitectures = "native";
            ReadWritePaths = [cfg.stateDir];

            # systemd's default (90s) SIGTERM timeout is far longer than the
            # server needs to stop; don't make a restart wait on it.
            TimeoutStopSec = 15;
          }
          // lib.optionalAttrs (credentials != {}) {
            # Deliver each secret as a systemd credential and read it into the
            # matching environment variable. Kept out of the Nix store, and out
            # of the environment of every unrelated process.
            LoadCredential = credentialLines;
            ExecStart = lib.mkForce (
              pkgs.writeShellScript "busypanel-serve" ''
                ${lib.concatMapStrings (name: ''
                  if [ -r "$CREDENTIALS_DIRECTORY/${name}" ]; then
                    export ${name}="$(cat "$CREDENTIALS_DIRECTORY/${name}")"
                  fi
                '') credentialNames}
                exec ${cfg.package}/bin/busypanel web --host ${cfg.host} --port ${toString cfg.port}
              ''
            );
          };
        };
      };
    };
  };
}
