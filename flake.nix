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
