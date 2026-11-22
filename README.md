# Pinha 3D — Processador de arquivos `.3mf` + telemetria da impressora

Ferramentas em **Python** que fazem a ponte entre o chão de fábrica de uma operação de
impressão 3D e o sistema de gestão: extraem dados de custo e material direto dos arquivos
de fatiamento e sincronizam o estado da impressora em tempo real.

Fazem parte do sistema interno da **Pinha 3D**, um pequeno negócio de manufatura aditiva
(impressão 3D de peças personalizadas). Este repositório contém a parte de **integração**
— o restante do sistema (frontend e banco) é privado.

## Contexto do problema

Na Pinha 3D, todo o custo de produção é medido em **gramas de filamento** e em **tempo de
impressão**. Esses números ficam "presos" dentro do arquivo `.3mf` que o slicer gera, e
digitar tudo à mão a cada peça é lento e dá erro. Além disso, operando a impressora à
distância, é útil saber se ela está imprimindo, parada ou com problema sem ir até o
equipamento. Estas ferramentas resolvem as duas coisas.

## Componentes

### 1. `parser_3mf.py` — extração de dados de fatiamento

Um arquivo `.3mf` é, na prática, um **ZIP de arquivos XML** (padrão 3MF Consortium, com
extensões de cada fabricante). O parser abre o pacote e lê os metadados de fatiamento
gerados por **Creality Print** (e compatíveis como Bambu Studio, PrusaSlicer e OrcaSlicer):

- tempo de impressão estimado;
- consumo de filamento por slot/material (peça + purga), em gramas;
- material e cor de cada slot;
- dimensões da peça (bounding box).

Quando o arquivo não traz esses metadados, há um **fallback geométrico**: o volume real da
malha é calculado pelo método dos **tetraedros com sinal** (teorema do volume) e convertido
em gramas pela densidade do material.

Usa **apenas a biblioteca padrão** do Python (`zipfile`, `xml.etree.ElementTree`, `json`) —
sem dependências externas.

```bash
# Saída legível
python3 parser_3mf.py meu-modelo.3mf

# Saída JSON (para integração com o frontend)
python3 parser_3mf.py meu-modelo.3mf --json --pretty

# Densidade personalizada (ex.: PETG = 1.27 g/cm³)
python3 parser_3mf.py meu-modelo.3mf --density 1.27 --json
```

Exemplo de saída:

```json
{
  "source_file": "pinheiro-natalino.3mf",
  "source": "creality_print",
  "print_time_minutes": 142,
  "total_filament_g": 48.3,
  "filaments": [
    { "slot": 0, "material": "PLA", "color_hex": "#2D6A2D", "weight_net_g": 38.1, "weight_purge_g": 10.2 }
  ],
  "bounding_box_mm": { "x": 60.0, "y": 60.0, "z": 120.0 },
  "warnings": []
}
```

O campo `source` indica a confiança do dado: `creality_print` (metadados completos do
slicer), `config_ini` (config embutido), `geometric_estimate` (estimativa pela geometria)
ou `unknown`.

### 2. `sync_printer.py` — telemetria em tempo real

Lê o status da impressora (Creality Sparkx i7) pela API **Klipper/Moonraker** na rede local
e publica a telemetria em um backend **Supabase** (tabela `system_settings`), de onde o
frontend lê o estado atual: imprimindo/parada, progresso, temperaturas de bico e mesa,
arquivo em andamento e última atualização. Em caso de falha de conexão, registra o estado
`offline` com a mensagem de erro.

```bash
python3 sync_printer.py          # loop contínuo (a cada 3s)
python3 sync_printer.py --once   # uma única leitura
```

## Configuração

As credenciais **não** ficam no código. Copie o modelo e preencha o `.env` local
(ignorado pelo Git):

```bash
cp .env.example .env
# edite .env com o IP da impressora e as credenciais do Supabase
```

| Variável | Descrição |
|---|---|
| `PRINTER_IP` | IP da impressora na rede local |
| `PRINTER_PORT` | porta do Moonraker (padrão `7125`) |
| `SUPABASE_URL` | URL do projeto Supabase |
| `SUPABASE_SERVICE_KEY` | chave de serviço do Supabase |

## Stack

Python 3.8+ (biblioteca padrão), formato 3MF (ZIP + XML), API Klipper/Moonraker, Supabase
(PostgreSQL + REST).

## Licença

[MIT](LICENSE) — André Martins da Silva.
