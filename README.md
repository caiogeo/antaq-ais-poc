# antaq-ais-poc

Alimentador de posições AIS do Brasil para uma prova de conceito (POC) de demonstração da Imagem Geosistemas para a ANTAQ.

A cada 5 minutos, uma execução do GitHub Actions escuta por cerca de 4 minutos o fluxo do [aisstream.io](https://aisstream.io)
(cobertura limitada aos receptores disponíveis, sem garantia de completude) no retângulo do Brasil e grava no ArcGIS Online:

- a última posição de cada embarcação vista nos últimos 15 minutos;
- o rastro dos últimos 15 minutos;
- uma linha de situação (hora da atualização e totais).

Dados auxiliares em `dados/`: malha do Brasil (IBGE, API de malhas, qualidade mínima) e áreas portuárias (OpenStreetMap, ODbL).

As chaves (`ARCGIS_API_KEY`, com acesso a um único serviço, e `AISSTREAM_KEY`) ficam nos segredos do repositório e não aparecem
no código nem nos registros de execução. Uso restrito à demonstração; não é fonte oficial de dados de tráfego aquaviário.
