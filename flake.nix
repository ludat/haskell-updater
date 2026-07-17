{
  description = "Dev shell for the Haskell Monad-of-No-Return refactor runner";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3   # runs agent/refactor.py (stdlib only)
            git       # git apply
            nushell   # local scripting in this env
            argo-workflows
            go        # builds the static build-server binary
          ];
        };
      });
}
