{
  description = "Arma 3 Helm chart development and validation";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              kubernetes-helm
              kubectl
              kubeconform
              actionlint
              (python3.withPackages (ps: [
                ps.pyyaml
                ps.jsonschema
              ]))
              git
              gh
              jq
              curl
              nixfmt
            ];
          };
        }
      );
      checks = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          chart =
            pkgs.runCommand "arma3-chart-check"
              {
                nativeBuildInputs = with pkgs; [
                  kubernetes-helm
                  actionlint
                  (python3.withPackages (ps: [
                    ps.pyyaml
                    ps.jsonschema
                  ]))
                ];
              }
              ''
                export HOME="$TMPDIR"
                cp -r ${self} source
                chmod -R u+w source
                cd source
                bash scripts/check.sh
                touch "$out"
              '';
        }
      );
    };
}
