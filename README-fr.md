# VTherm Inhibition

[Read this documentation in English](README.md)

Plugin Home Assistant qui permet à un [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) de **se retirer pendant qu'un autre équipement prend le relais** dans la pièce : radiateur électrique alimenté par le surplus photovoltaïque, climatisation qui chauffe ou refroidit, personne qui chauffe manuellement à la vanne...

Il implémente le fournisseur d'inhibition de l'[issue #2087](https://github.com/jmcollin78/versatile_thermostat/issues/2087) (« retrait contrôlé du VTherm »). Le plugin se contente de **détecter** la condition et de signaler un état confirmé ; le cœur de Versatile Thermostat fait le reste : il résout le mode HVAC effectif à `OFF` (raison `hvac_off_inhibition`), suspend la régulation et la réparation, envoie au plus un arrêt physique (selon la politique ci-dessous) et reprend l'état demandé **courant** à la fin de l'inhibition. Le plugin ne commande jamais le VTherm ni ses sous-jacents et ne modifie jamais le mode, le preset ou la consigne demandés.

## Fonctionnement

Pour chaque VTherm configuré, le plugin observe une ou plusieurs entités **sources** et calcule un état :

| État | Signification | Effet dans VTherm |
| --- | --- | --- |
| `active` | Un autre équipement a pris le relais (confirmé). | Retrait, `OFF`, au plus un arrêt. |
| `inactive` | Aucune source active (confirmé). | Fonctionnement normal. |
| `unknown` | Le plugin ne peut pas savoir (sources indisponibles au-delà du délai de grâce, plugin arrêté ou en rechargement). | Retrait prudent, aucune reprise avant confirmation de `inactive`. |

### Sources

| Domaine | Inhibe lorsque |
| --- | --- |
| `binary_sensor`, `input_boolean`, `switch` | son état est `on` |
| `climate` | son mode HVAC fait partie des *modes HVAC inhibants* (par défaut : `heat`, `cool`, `heat_cool`, `auto`, `dry`, c'est-à-dire tout sauf `off` et `fan_only`) |

Une source absente, `unavailable` ou `unknown` est *indisponible* ; tout autre état d'une source on/off (ni `on` ni `off`) est aussi considéré comme indisponible.

### Règle d'agrégation (plusieurs sources)

Les sources sont combinées par un **OU** à trois valeurs :

1. au moins une source disponible est active → `active`, même si d'autres sources sont indisponibles ;
2. sinon, au moins une source est indisponible → `unknown` (sous réserve du délai de grâce ci-dessous) ;
3. sinon (toutes les sources disponibles et inactives) → `inactive`.

### Délais (anti-rebond)

Seuls les états confirmés sont transmis à VTherm ; un changement brut ou une temporisation en cours ne le sont jamais.

- **Délai d'activation** : l'agrégat doit rester `active` pendant cette durée avant que `active` soit confirmé.
- **Délai de désactivation** : l'agrégat doit rester `inactive` pendant cette durée avant que `inactive` soit confirmé.
- Tout changement de l'agrégat annule la temporisation en cours ; si le nouvel agrégat diffère de l'état confirmé, une nouvelle temporisation repart de zéro. Un retour à l'état confirmé avant la fin (oscillation) l'annule sans rien transmettre.
- Une mise à jour d'attributs seule (même état) ne relance pas la temporisation.
- Un délai nul confirme immédiatement.

### Délai de grâce en cas d'indisponibilité

Les ponts HomeKit et les équipements Zigbee ou Wi-Fi passent souvent à `unavailable` quelques secondes. Lorsque l'agrégat devient `unknown`, le plugin **conserve le dernier état confirmé** pendant le *délai de grâce* ; `unknown` n'est transmis qu'à son expiration. Si les sources reviennent avant, rien n'est transmis (même état) ou le délai d'activation/désactivation normal démarre à leur retour (état différent). Avec un délai de grâce de `0`, `unknown` est transmis immédiatement. Le retour depuis un `unknown` transmis utilise aussi le délai d'activation/désactivation.

### Démarrage, redémarrage et rechargement

- Le **premier** agrégat connu après le démarrage du plugin (démarrage de Home Assistant, rechargement de l'entrée) est confirmé **immédiatement**, sans délai d'activation/désactivation, afin que VTherm reprenne (ou reste retiré) sans attendre. Les délais s'appliquent à tous les changements suivants.
- Si les sources ne sont pas encore disponibles à ce moment-là, l'état est `unknown`, sauf si le délai de grâce n'est pas nul et qu'un état avait été confirmé avant le redémarrage : cet état **restauré** (sauvegardé par le capteur binaire d'inhibition) est alors conservé au plus pendant le délai de grâce, compté depuis le démarrage du plugin. Le premier agrégat réel le remplace immédiatement.
- L'interrupteur de contournement conserve son état après un redémarrage.

### Contournement

Chaque VTherm dispose d'un **interrupteur de contournement**. Tant qu'il est allumé, toutes les sources sont ignorées et le plugin transmet **immédiatement** un `inactive` confirmé (les temporisations sont annulées, les sources indisponibles n'ont pas d'effet) : VTherm régule normalement. À l'extinction, les sources sont réévaluées et les délais normaux s'appliquent (par exemple, une source active est confirmée après le délai d'activation).

### Politique d'arrêt physique

| Valeur | Comportement à l'inhibition |
| --- | --- |
| `turn_off` (défaut) | VTherm se retire et éteint une fois ses sous-jacents. |
| `do_not_turn_off` | VTherm se retire (mode effectif `OFF`, ni régulation ni réparation) mais n'envoie aucun `turn_off` : les équipements restent en l'état, par exemple une vanne réglée à la main. |

Une valeur absente ou invalide vaut `turn_off`. La politique s'applique aussi à l'arrêt de précaution que VTherm peut envoyer pendant un état `unknown`.

## Prérequis

- Home Assistant 2026.7.1 ou supérieur.
- [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) avec la prise en charge des fournisseurs d'inhibition (issue #2087).
- `vtherm_api` version `0.6.0` ou supérieure (installé automatiquement).

## Installation

### Via HACS

1. Dans HACS, ajoutez `https://github.com/alexlenk/vtherm_inhibition` comme dépôt personnalisé de type *Intégration*.
2. Installez `VTherm Inhibition`.
3. Redémarrez Home Assistant.
4. Allez dans **Paramètres > Appareils et services > Ajouter une intégration** et ajoutez `VTherm Inhibition`.

### Installation manuelle

1. Copiez [custom_components/vtherm_inhibition](custom_components/vtherm_inhibition) dans le dossier `custom_components` de Home Assistant.
2. Redémarrez Home Assistant.
3. Ajoutez `VTherm Inhibition` depuis **Paramètres > Appareils et services**.

## Configuration

Ajoutez une entrée d'intégration **par VTherm** : choisissez d'abord le Versatile Thermostat, puis ses sources et réglages. Tout sauf le VTherm se modifie ensuite via **Configurer**.

| Option (clé) | Type | Défaut | Description |
| --- | --- | --- | --- |
| Versatile Thermostat (`target_entity`) | entité climate de `versatile_thermostat` | obligatoire | Le VTherm à inhiber, mémorisé par son identifiant unique stable (`vtherm_unique_id`) et son entity ID (`vtherm_entity_id`). Une entrée par VTherm. |
| Sources (`source_entities`) | liste de `binary_sensor` / `input_boolean` / `switch` / `climate` | obligatoire, au moins une | Sources combinées par OU. Le VTherm lui-même est refusé. |
| Modes HVAC inhibants (`climate_hvac_modes`) | liste parmi `heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only` | `heat`, `cool`, `heat_cool`, `auto`, `dry` | Uniquement pour les sources `climate`. |
| Délai d'activation (`activation_delay`) | secondes, 0–86400 | `30` | Anti-rebond avant confirmation de `active`. |
| Délai de désactivation (`deactivation_delay`) | secondes, 0–86400 | `60` | Anti-rebond avant confirmation de `inactive`. |
| Délai de grâce si indisponible (`unavailable_grace_period`) | secondes, 0–86400 | `300` | Durée de conservation du dernier état confirmé lorsque des sources sont indisponibles. |
| À l'inhibition (`physical_stop_policy`) | `turn_off` / `do_not_turn_off` | `turn_off` | Politique d'arrêt physique, voir ci-dessus. |

L'enregistrement des options recharge l'entrée, puis le VTherm ciblé rattache le nouveau fournisseur (voir [Cycle de vie](#cycle-de-vie)).

### Exemples

- **Radiateur électrique sur surplus PV dans la salle de bain** : source `climate.bathroom_electric_heater_heater`, mode inhibant `heat`, politique `turn_off`.
- **Pièce avec climatisation** (chauffe sur électricité bon marché/solaire, refroidit le soir) : source `climate.<pièce>_ac`, modes inhibants par défaut, politique `turn_off`.
- **Bureau où quelqu'un chauffe à la main à la vanne** : un `input_boolean` (ou tout capteur binaire détectant le mode manuel) comme source, politique `do_not_turn_off` pour ne pas éteindre la vanne.
- **Remplacer un capteur binaire « chauffage bloqué » utilisé comme capteur de fenêtre du VTherm** : utilisez ce capteur (ou directement les entités climate dont il dépend) comme source ici, et rendez la fonction fenêtre du VTherm à un vrai contact de fenêtre ou à la détection automatique.

## Entités

Pour chaque VTherm configuré, rattachées à l'appareil du VTherm (ou à un appareil au nom de l'entrée si le VTherm n'a pas d'appareil) :

| Entité | Identifiant unique | Description |
| --- | --- | --- |
| `binary_sensor.<vtherm>_inhibition` | `<vtherm_unique_id>_inhibition` | `on` = `active` confirmé, `off` = `inactive` confirmé, `unknown` = `unknown`. |
| `switch.<vtherm>_inhibition_bypass` | `<vtherm_unique_id>_inhibition_bypass` | Contournement (entité de configuration). |

Attributs du capteur binaire :

| Attribut | Description |
| --- | --- |
| `status` | État confirmé : `active`, `inactive` ou `unknown`. |
| `raw_status` | Agrégat courant, avant délais. |
| `pending_status`, `pending_until` | État qu'une temporisation en cours confirmera, et quand (UTC). `null` sans temporisation. |
| `last_change` | Heure du dernier changement confirmé. |
| `sources` | Par source : son `state` et sa `contribution` (`active`, `inactive`, `unavailable`). |
| `bypass` | État du contournement. |
| `physical_stop_policy` | Politique effective. |
| `climate_hvac_modes`, `activation_delay`, `deactivation_delay`, `unavailable_grace_period` | Réglages effectifs. |
| `vtherm_entity_id` | VTherm ciblé. |
| `provider_attached` | `true` dès que VTherm a rattaché le fournisseur. |

## Cycle de vie

- La fabrique de fournisseurs est enregistrée dans l'API VTherm à la mise en place de la première entrée et retirée au déchargement de la dernière. Elle n'accepte un VTherm que si une entrée cible son identifiant unique (l'entity ID n'est utilisé que si le VTherm n'expose pas d'identifiant unique).
- Au démarrage de Home Assistant, VTherm découvre lui-même la fabrique (son `async_startup` s'exécute une fois Home Assistant démarré).
- Lorsqu'une entrée est ajoutée, modifiée ou réactivée alors que Home Assistant tourne, le plugin commence par surveiller ses sources, puis enregistre la fabrique : VTherm rattache aussitôt le fournisseur et obtient immédiatement un statut confirmé. Si la fabrique était déjà enregistrée (une autre entrée est chargée), le plugin recharge plutôt le VTherm ciblé pour qu'il rattache le fournisseur.
- Au déchargement ou au rechargement d'une entrée, le fournisseur rattaché transmet `unknown` avant de se détacher : VTherm reste retiré par prudence jusqu'à ce qu'un fournisseur confirme `inactive`. Lorsqu'une entrée est **supprimée ou désactivée**, le VTherm est rechargé sans fournisseur et fonctionne comme avant.
- Conséquence pour un VTherm qui n'est **pas** inhibé à ce moment : avec la politique `turn_off`, ce `unknown` fait envoyer par VTherm son arrêt de précaution ; l'équipement est donc brièvement éteint puis rallumé dès que le nouveau fournisseur confirme `inactive` (modification des options, rechargement) ou dès que le VTherm est rechargé sans fournisseur (suppression, désactivation).

## Limites

- Un seul fournisseur d'inhibition par VTherm (V1 de la conception VTherm) ; regroupez plutôt plusieurs sources dans une entrée.
- Le plugin ne commande aucun équipement : il indique seulement à VTherm quand se retirer.

## Développement

```bash
pip install -r requirements_test.txt
pytest -q tests
```

Les tests utilisent un faux cœur VTherm qui ne s'appuie que sur le contrat `vtherm_api`.

### Tests de bout en bout

`tests_e2e/` exécute ce plugin avec le **vrai** cœur Versatile Thermostat (support de l'inhibition, issue #2087) dans le banc de test de Home Assistant : vrais VTherms (over_climate, over_switch TPI) avec des équipements sous-jacents simulés, vraies entrées du plugin, minuteries, rechargements et redémarrages simulés. Le cœur provient d'un dépôt local ; ses dépendances de test (`requirements_dev.txt` du cœur) doivent être installées dans le même environnement :

```bash
VTHERM_CORE_PATH=/chemin/vers/versatile_thermostat pytest -q tests_e2e
```

Sans `VTHERM_CORE_PATH` (ou s'il ne désigne pas un dépôt `versatile_thermostat`), ces modules sont ignorés (skipped).
