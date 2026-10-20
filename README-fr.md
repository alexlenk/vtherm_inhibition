# VTherm Inhibition

[Read this documentation in English](README.md)

Plugin Home Assistant qui permet à un [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) de **se retirer pendant qu'un autre équipement prend le relais** dans la pièce : radiateur électrique alimenté par le surplus photovoltaïque, climatisation qui chauffe ou refroidit, personne qui chauffe manuellement à la vanne...

C'est le plugin d'inhibition de l'[issue #2087](https://github.com/jmcollin78/versatile_thermostat/issues/2087) (« inhibition contrôlée d'un VTherm », conception v2). Le plugin porte toute la logique métier : sources, délais, délai de grâce, contournement, état, cycle de vie et l'option d'arrêt d'entrée. Il indique à Versatile Thermostat quoi faire au moyen des deux opérations de l'API VTherm, `set_inhibition(stop_underlyings)` et `unset_inhibition()`. Le cœur de Versatile Thermostat résout alors le mode HVAC effectif à `OFF` (raison `hvac_off_inhibition`), cesse de réguler et de réparer, bloque toute commande vers ses sous-jacents (sauf l'arrêt d'entrée optionnel) et, à la levée de l'inhibition, réconcilie les équipements avec l'état demandé **courant**. Le plugin ne commande jamais lui-même les sous-jacents et ne modifie jamais le mode, le preset ou la consigne demandés.

## Fonctionnement

Pour chaque VTherm configuré, le plugin observe une ou plusieurs entités **sources** et calcule un état :

| État | Signification | Appel à Versatile Thermostat |
| --- | --- | --- |
| `active` | Un autre équipement a pris le relais (confirmé). | `set_inhibition(stop_underlyings)` |
| `inactive` | Aucune source active (confirmé). | `unset_inhibition()` |
| `unknown` | Le plugin ne peut pas savoir (sources indisponibles au-delà du délai de grâce). | `set_inhibition(stop_underlyings)` : le VTherm reste inhibé jusqu'à confirmation de `inactive`. |

Seuls les états **confirmés** donnent lieu à un appel ; `unset_inhibition` n'est appelé qu'une fois `inactive` confirmé.

### Sources

| Domaine | Inhibe lorsque |
| --- | --- |
| `binary_sensor`, `input_boolean`, `switch` | son état est `on` |
| `climate` | son mode HVAC fait partie des *modes HVAC inhibants* (par défaut : `heat`, `cool`, `heat_cool`, `auto`, `dry`, c'est-à-dire tout sauf `off` et `fan_only`) |

Une source absente, `unavailable` ou `unknown` est *indisponible* ; tout autre état d'une source on/off (ni `on` ni `off`) est aussi considéré comme indisponible.

Les sources qui reboucleraient sur le plugin sont refusées par la configuration : le VTherm lui-même, toutes les entités qu'il commande (sous-jacents, ouverture/fermeture de vanne, entités de calibrage et de synchronisation) et les entités du plugin (capteurs d'inhibition, interrupteurs de contournement). Une source renommée est suivie automatiquement ; une source qui n'existe plus déclenche une réparation (**Paramètres > Système > Réparations**) jusqu'à son retour ou son remplacement.

### Règle d'agrégation (plusieurs sources)

Les sources sont combinées par un **OU** à trois valeurs :

1. au moins une source disponible est active → `active`, même si d'autres sources sont indisponibles ;
2. sinon, au moins une source est indisponible → `unknown` (sous réserve du délai de grâce ci-dessous) ;
3. sinon (toutes les sources disponibles et inactives) → `inactive`.

### Délais (anti-rebond)

- **Délai d'activation** : l'agrégat doit rester `active` pendant cette durée avant que `active` soit confirmé.
- **Délai de désactivation** : l'agrégat doit rester `inactive` pendant cette durée avant que `inactive` soit confirmé.
- Tout changement de l'agrégat annule la temporisation en cours ; si le nouvel agrégat diffère de l'état confirmé, une nouvelle temporisation repart de zéro. Un retour à l'état confirmé avant la fin (oscillation) l'annule sans aucun appel.
- Une mise à jour d'attributs seule (même état) ne relance pas la temporisation.
- Un délai nul confirme immédiatement.

### Délai de grâce en cas d'indisponibilité

Les ponts HomeKit et les équipements Zigbee ou Wi-Fi passent souvent à `unavailable` quelques secondes. Lorsque l'agrégat devient `unknown`, le plugin **conserve le dernier état confirmé** pendant le *délai de grâce* ; `unknown` n'est confirmé (et le VTherm inhibé) qu'à son expiration. Sans lui, chaque courte coupure inhiberait le VTherm et, avec l'arrêt d'entrée, éteindrait ses équipements. Un délai de grâce de `0` confirme `unknown` immédiatement ; **toute** coupure d'une source inhibe alors aussitôt le VTherm (avec l'arrêt d'entrée, ses équipements sont éteints), y compris une source renommée (l'ancienne entité disparaît avant que la nouvelle existe) ou qui arrive lentement après un redémarrage de Home Assistant. Gardez au moins quelques secondes de délai de grâce, sauf si c'est vraiment voulu.

Le délai de grâce est mesuré depuis la **première** indisponibilité d'un *épisode d'indisponibilité*. L'épisode ne se termine que lorsque l'agrégat est de nouveau connu **et** confirmé (par exemple la source revient avec l'état confirmé, ou un nouvel état a passé son délai). Une source qui oscille entre `unavailable` et une autre valeur ne peut donc pas maintenir un état périmé au-delà du délai de grâce, et un rechargement de l'entrée ou un redémarrage pendant l'épisode n'accorde pas un second délai (le début de l'épisode est sauvegardé avec le capteur binaire d'inhibition). Si les sources reviennent avant, rien ne se passe (même état) ou le délai d'activation/désactivation normal démarre à leur retour (état différent). Le retour depuis un `unknown` confirmé utilise aussi le délai d'activation/désactivation.

### Démarrage, redémarrage et rechargement

- Le **premier** agrégat connu après le démarrage du plugin (démarrage de Home Assistant, rechargement de l'entrée) est confirmé **immédiatement**, sans délai d'activation/désactivation. Les délais s'appliquent à tous les changements suivants.
- Si les sources ne sont pas encore disponibles à ce moment-là, l'état est `unknown`, sauf si le délai de grâce n'est pas nul et qu'un état avait été confirmé avant le redémarrage : cet état **restauré** (sauvegardé par le capteur binaire d'inhibition) est alors conservé au plus pendant le délai de grâce, compté depuis le démarrage du plugin, ou depuis le début de l'épisode d'indisponibilité déjà en cours avant le rechargement/redémarrage. Le premier agrégat réel le remplace immédiatement.
- L'interrupteur de contournement conserve son état après un redémarrage.
- Un redémarrage ou un rechargement ne libère jamais à lui seul un VTherm inhibé : l'état restauré (ou évalué en premier) est réappliqué, et `unset_inhibition` n'est appelé qu'une fois `inactive` confirmé. Voir [Appels à Versatile Thermostat](#appels-à-versatile-thermostat).

### Contournement

Chaque VTherm dispose d'un **interrupteur de contournement**. Tant qu'il est allumé, toutes les sources sont ignorées et l'état est **immédiatement** un `inactive` confirmé (les temporisations sont annulées, les sources indisponibles n'ont pas d'effet) : le VTherm est libéré et régule normalement. À l'extinction, les sources sont réévaluées et les délais normaux s'appliquent (par exemple, une source active est confirmée après le délai d'activation).

### Arrêt d'entrée (`stop_underlyings`)

| Valeur | Comportement au début de l'inhibition |
| --- | --- |
| activé (défaut) | Le VTherm se retire et éteint une fois ses sous-jacents. |
| désactivé | Le VTherm se retire (mode effectif `OFF`, ni régulation, ni réparation, ni commande) mais n'envoie rien : les équipements restent en l'état, par exemple une vanne réglée à la main. |

L'option est transmise à `set_inhibition` au début d'une inhibition. La modifier pendant que le VTherm est inhibé ne déclenche aucun appel : elle s'applique à partir de l'inhibition suivante.

## Appels à Versatile Thermostat

- Le VTherm est identifié par son identifiant unique stable, résolu à chaque appel en son entity ID **courant** : un renommage de l'entité du VTherm est suivi (l'entrée est mise à jour). Si aucun VTherm avec cet identifiant unique n'est enregistré (supprimé, ou pas encore créé), le VTherm est considéré indisponible : l'entity ID mémorisé n'est jamais utilisé à la place, car il pourrait désigner une autre entité.
- Les appels sont faits une fois que l'entité du VTherm existe et que le VTherm a restauré son état au démarrage (le cœur refuse les appels avant). Le plugin n'attend pas que le VTherm soit prêt (chaque sous-jacent ayant publié un état) : le cœur diffère l'arrêt d'entrée et la réconciliation de sortie jusque-là. Un VTherm inhibé avant un redémarrage reste inhibé grâce à son état restauré (pas de second arrêt d'entrée) ; un VTherm qui ne l'était pas démarre normalement jusqu'à l'arrivée du `set` du plugin (vérifié à chaque changement d'état du VTherm et toutes les 5 secondes). Pendant cette attente, `waiting_for_vtherm_start` vaut `true` ; tant que l'entité du VTherm n'existe pas, l'appel en attente est signalé par l'attribut `retry_pending` et effectué dès qu'elle apparaît.
- À chaque fois que le VTherm devient disponible (démarrage de Home Assistant, rechargement du VTherm) et à chaque mise en place de l'entrée du plugin (rechargement, redémarrage), l'appel correspondant à l'état courant est refait : `set_inhibition` est idempotent dans le cœur (pas de second arrêt) et `unset_inhibition` ne fait rien hors inhibition.
- Des états demandant le même appel (`active` ↔ `unknown`) ne déclenchent pas de nouvel appel.
- Le plugin compare aussi l'état d'inhibition publié par le VTherm (`specific_states.inhibition`) à son dernier appel réussi. Lorsque `unset_inhibition` atteint un VTherm démarré mais pas encore prêt (un sous-jacent n'a pas publié d'état), le cœur l'accepte et réconcilie dès qu'il est prêt ; si cette réconciliation échoue, le VTherm est de nouveau inhibé. Le plugin voit alors un VTherm inhibé alors qu'il a confirmé `inactive`, et retente `unset_inhibition` comme un appel en échec. De même, un VTherm qui se déclare non inhibé après un `set_inhibition` réussi est de nouveau inhibé.
- Les appels ne se chevauchent jamais : un changement d'état pendant un appel est appliqué juste après.
- **Échecs et nouvelles tentatives** : lorsqu'un appel échoue (par exemple le cœur n'a pas pu réconcilier les équipements à la sortie de l'inhibition : il lève une erreur et garde le VTherm inhibé), il est retenté après 30 s, 60 s, 120 s, puis toutes les 5 minutes, tant que l'état demande toujours le même appel. Un changement d'état annule la tentative et effectue le nouvel appel. Si `unset_inhibition` échoue encore après la 3e nouvelle tentative, une réparation signale que le VTherm reste inhibé ; elle disparaît en cas de succès ou de changement d'état. Un Versatile Thermostat sans prise en charge de l'inhibition (l'entité du VTherm n'a pas les opérations d'inhibition) est signalé par une réparation et l'appel est refait au redémarrage du VTherm ; toute autre erreur, y compris une erreur interne au cœur, est retentée.

## Prérequis

- Home Assistant 2026.7.1 ou supérieur.
- [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) : **10.6.1 ou ultérieure**, la première version stable incluant l'inhibition (#2087, [#2101](https://github.com/jmcollin78/versatile_thermostat/pull/2101)). Les versions antérieures ne prennent pas en charge l'inhibition.
- `vtherm_api` **≥ 0.6.2** (publié sur PyPI, installé automatiquement).

Avec un Versatile Thermostat plus ancien, les entrées se chargent mais le cœur refuse les appels : une réparation indique les versions minimales et les VTherm ne sont pas touchés.

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

### Mise à jour depuis 0.1.x

Les entrées sont migrées automatiquement : l'ancienne *politique d'arrêt physique* `turn_off` (ou absente) devient `stop_underlyings` activé, `do_not_turn_off` devient `stop_underlyings` désactivé.

## Configuration

Ajoutez une entrée d'intégration **par VTherm** : choisissez d'abord le Versatile Thermostat, puis ses sources et réglages. Tout sauf le VTherm se modifie ensuite via **Configurer**.

| Option (clé) | Type | Défaut | Description |
| --- | --- | --- | --- |
| Versatile Thermostat (`target_entity`) | entité climate de `versatile_thermostat` | obligatoire | Le VTherm à inhiber, mémorisé par son identifiant unique stable (`vtherm_unique_id`) et son entity ID (`vtherm_entity_id`, tenu à jour). Une entrée par VTherm, nommée d'après lui avec sa pièce devant (par ex. « Salle de bain Chauffage ») sauf si le nom contient déjà la pièce. |
| Sources (`source_entities`) | liste de `binary_sensor` / `input_boolean` / `switch` / `climate` | obligatoire, au moins une | Sources combinées par OU. Le VTherm lui-même est refusé. |
| Modes HVAC inhibants (`climate_hvac_modes`) | liste parmi `heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only` | `heat`, `cool`, `heat_cool`, `auto`, `dry` | Uniquement pour les sources `climate`. |
| Délai d'activation (`activation_delay`) | secondes, 0–86400 | `30` | Anti-rebond avant confirmation de `active`. |
| Délai de désactivation (`deactivation_delay`) | secondes, 0–86400 | `60` | Anti-rebond avant confirmation de `inactive`. |
| Délai de grâce si indisponible (`unavailable_grace_period`) | secondes, 0–86400 | `300` | Durée de conservation du dernier état confirmé lorsque des sources sont indisponibles. |
| Éteindre les équipements au début de l'inhibition (`stop_underlyings`) | booléen | activé | Arrêt d'entrée, voir ci-dessus. |

L'enregistrement des options les applique **à chaud**, sans recharger l'entrée ni le VTherm. Aucun appel n'est fait tant que l'état confirmé ne change pas.

### Exemples

- **Radiateur électrique sur surplus PV dans la salle de bain** : source `climate.bathroom_electric_heater_heater`, mode inhibant `heat`, arrêt d'entrée activé.
- **Pièce avec climatisation** (chauffe sur électricité bon marché/solaire, refroidit le soir) : source `climate.<pièce>_ac`, modes inhibants par défaut, arrêt d'entrée activé.
- **Bureau où quelqu'un chauffe à la main à la vanne** : un `input_boolean` (ou tout capteur binaire détectant le mode manuel) comme source, arrêt d'entrée désactivé pour ne pas éteindre la vanne.
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
| `stop_underlyings` | Option d'arrêt d'entrée transmise au prochain `set_inhibition`. |
| `bypass` | État du contournement. |
| `active_sources` | Sources actuellement actives. |
| `sources` | Par source : son `state` et sa `contribution` (`active`, `inactive`, `unavailable`). |
| `pending_status`, `pending_until` | État qu'une temporisation en cours confirmera, et quand (UTC). `null` sans temporisation. |
| `last_change` | Heure du dernier changement confirmé. |
| `unavailable_since` | Début de l'épisode d'indisponibilité en cours (UTC), `null` hors épisode. |
| `missing_sources` | Sources configurées qui n'existent plus. |
| `vtherm_entity_id` | Entity ID courant du VTherm ciblé. |
| `vtherm_available` | `true` tant que l'entité du VTherm existe. |
| `last_api_call`, `last_api_call_at` | Dernier appel effectué (`set` ou `unset`) et quand (UTC). |
| `last_error` | Erreur du dernier appel, `null` dès qu'un appel a réussi. |
| `retry_pending` | `true` tant que l'appel correspondant à l'état n'a pas encore réussi (VTherm indisponible ou pas encore démarré, ou nouvelle tentative programmée). |
| `next_retry_at` | Heure de la prochaine tentative (UTC), `null` si aucune n'est programmée. |
| `waiting_for_vtherm_start` | `true` tant que l'appel attend que le VTherm restaure son état au démarrage. |

Versatile Thermostat publie de son côté l'attribut `specific_states.inhibition` du VTherm (inhibition, garde des commandes, arrêt d'entrée demandé/effectué, dernier code d'erreur).

## Cycle de vie

- **Modification des options** : appliquée à chaud à l'entrée en cours. Un changement d'état brut dû aux nouvelles sources ou aux nouveaux modes HVAC suit les règles normales (délai d'activation/désactivation, délai de grâce). Une temporisation déjà en cours garde son heure de départ et son échéance suit le nouveau délai (confirmation immédiate s'il est déjà écoulé). Un changement de `stop_underlyings` s'applique à partir de l'inhibition suivante. Les entités reflètent immédiatement les nouveaux réglages.
- **Rechargement de l'entrée ou redémarrage de Home Assistant** : le déchargement ne fait aucun appel, un VTherm inhibé reste donc inhibé. À sa remise en place, l'entrée restaure son état et son contournement et refait l'appel correspondant dès que le VTherm est disponible et démarré.
- **Entrée déchargée ou désactivée** : aucun appel ; un VTherm inhibé **reste inhibé** (un déchargement ne signifie jamais `inactive`). Allumez le contournement avant de désactiver l'entrée si le VTherm doit reprendre, ou supprimez l'entrée.
- **Entrée supprimée** : `unset_inhibition` est appelé, le VTherm est libéré et fonctionne comme avant. Si le VTherm n'a pas encore démarré (démarrage de Home Assistant), l'appel est fait une fois qu'il a démarré, au plus tard 5 minutes après. Si cet appel échoue (par exemple Versatile Thermostat n'est pas chargé), une réparation indique que le VTherm peut rester inhibé et comment le libérer (voir ci-dessous) ; elle disparaît dès qu'une entrée pour ce VTherm est de nouveau mise en place.
- **Rechargement du VTherm** : le cœur restaure son état d'inhibition ; le plugin refait l'appel correspondant dès que l'entité du VTherm est de retour et a restauré son état.

### Récupération si le plugin est supprimé pendant qu'un VTherm est inhibé

Versatile Thermostat conserve une inhibition à travers les redémarrages jusqu'à ce qu'un plugin la lève. Si le code du plugin est supprimé (désinstallé) alors qu'une de ses entrées inhibe encore un VTherm, ce VTherm reste inhibé. Pour le libérer : réinstallez le plugin, redémarrez Home Assistant, puis supprimez l'entrée du plugin pour ce VTherm ; la suppression appelle `unset_inhibition`. Si l'entrée a déjà été supprimée pendant que Versatile Thermostat ne tournait pas (réparation *VTherm peut-être encore inhibé*), ajoutez de nouveau une entrée pour ce VTherm : elle le libère dès que ses sources sont inactives (ou allumez son contournement), puis supprimez-la. Supprimer les entrées **avant** de désinstaller le plugin évite cette situation.

## Dépannage

### Pourquoi mon VTherm s'est-il éteint ?

À chaque fois que le plugin demande à Versatile Thermostat de se retirer ou de reprendre, il écrit un événement dans le journal du VTherm (les journaux que Versatile Thermostat affiche pour chaque VTherm), avec la raison :

```
Bureau - ---------------------> NEW EVENT: Inhibition requested by VThermInhibition-Bureau (active source: binary_sensor.poele_allume) ---...
Bureau - ---------------------> NEW EVENT: Inhibition released by VThermInhibition-Bureau (no active source) ---...
```

La raison est la ou les sources actives, la ou les sources indisponibles (état `unknown`, après le délai de grâce), l'interrupteur de contournement allumé, ou l'absence de source active. Sur le VTherm lui-même, une inhibition apparaît comme le message `hvac_off_inhibition` et dans l'attribut `specific_states.inhibition` (voir plus haut).

### « RepairIncorrectStateManager: underlying ... was repaired » pendant que le VTherm est inhibé

Si vous manœuvrez à la main un sous-jacent pendant que son VTherm est inhibé (par exemple vous allumez l'interrupteur d'un radiateur), Versatile Thermostat constate que l'équipement n'est pas dans l'état attendu et écrit un avertissement comme :

```
WARNING [feature_repair_incorrect_state_manager] Thermostat Bureau - RepairIncorrectStateManager: underlying switch.radiateur_bureau was repaired. Consecutive repairs so far: 1
```

C'est normal et sans conséquence : la commande de réparation est bloquée par l'inhibition, **l'équipement reste comme vous l'avez réglé**. Si cela se répète sur 5 cycles consécutifs, Versatile Thermostat écrit aussi une erreur indiquant que les réparations sont suspendues (`maximum consecutive repair attempts (5) reached`) puis réessaie plus tard ; c'est également sans conséquence pendant une inhibition. À la fin de l'inhibition, le VTherm reprend le contrôle de ses équipements.

## Limites

- Une seule entrée d'inhibition par VTherm ; regroupez plutôt plusieurs sources dans une entrée.
- Le plugin ne commande aucun équipement : il indique seulement à Versatile Thermostat quand se retirer et quand reprendre.

## Développement

```bash
pip install -r requirements_test.txt
pytest -q tests
```

Les tests unitaires utilisent une fausse API du cœur qui enregistre les appels `set_inhibition` / `unset_inhibition`.

### Publier une version

Modifiez `version` dans `custom_components/vtherm_inhibition/manifest.json` (et `pyproject.toml`) sur `main` : le workflow Release crée alors l'étiquette de cette version et publie la release avec son zip, sauf si la version est déjà étiquetée. Pousser une étiquette `X.Y.Z`, ou *Run workflow* avec une version, publie aussi.

### Tests de bout en bout

`tests_e2e/` exécute ce plugin avec le **vrai** cœur Versatile Thermostat (support de l'inhibition, issue #2087) dans le banc de test de Home Assistant : vrais VTherms (over_climate, over_switch TPI) avec des équipements sous-jacents simulés, vraies entrées du plugin, minuteries, rechargements et redémarrages simulés ; les appels de service reçus par les équipements sont comptés. Le cœur provient d'un dépôt local (le dépôt ou son dossier `custom_components`) ; ses dépendances de test (`requirements_dev.txt` du cœur) doivent être installées dans le même environnement :

```bash
VTHERM_CORE_PATH=/chemin/vers/versatile_thermostat pytest -q tests_e2e
```

Sans `VTHERM_CORE_PATH` (ou s'il ne désigne pas un dépôt `versatile_thermostat`), ces modules sont ignorés (skipped). En CI, `.github/workflows/e2e.yml` les exécute par défaut avec `jmcollin78/versatile_thermostat` `main` (dépôt et référence du cœur au choix lors d'un lancement manuel) ; il ne bloque pas la CI tant qu'aucune version de Versatile Thermostat avec l'inhibition n'existe.
