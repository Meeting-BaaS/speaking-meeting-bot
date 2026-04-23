{
  description = "Development shell for speaking-meeting-bot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
        };

        runtimeLibraries = with pkgs; [
          openssl
          ffmpeg
          portaudio
          libsndfile
          libopus
          zlib
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python311
            poetry
            git
            pkg-config
            ruff
          ] ++ runtimeLibraries;

          POETRY_VIRTUALENVS_IN_PROJECT = "true";
          POETRY_VIRTUALENVS_PREFER_ACTIVE_PYTHON = "true";
          PYTHON_KEYRING_BACKEND = "keyring.backends.null.Keyring";

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath (runtimeLibraries ++ [
              pkgs.stdenv.cc.cc.lib
            ])}:''${LD_LIBRARY_PATH:-}"

            echo "Python: $(python --version)"
            echo "Poetry: $(poetry --version)"
            echo ""
            echo "Run: poetry install"
          '';
        };
      });
}
